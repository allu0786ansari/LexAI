"""
LexAI evaluation CLI (run from backend/):

  python -m lexai.evaluation validate                     check every golden item against the index
  python -m lexai.evaluation retrieval --split dev --configs hybrid,dense,bm25,no-routing
  python -m lexai.evaluation answers --split dev --limit 40
  python -m lexai.evaluation export-fixture               snapshot for the CI gate (needs the models)
  python -m lexai.evaluation offline [--write-baseline]   CI gate: retrieval on the snapshot, no models
  python -m lexai.evaluation report                       eval/reports/<commit>/report.md
  python -m lexai.evaluation lock                         lock the test split (after validation)

Results go to eval/reports/<git commit>/ as JSON plus one Markdown report.
Exit code 1 when validation finds problems or the CI gate fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lexai.api.container import Container, build_chat
from lexai.core.settings import REPO_ROOT, Settings, get_settings
from lexai.evaluation import report
from lexai.evaluation.dataset import GoldenItem, load_dataset, load_items, validate_item, write_lock
from lexai.evaluation.fixture import (
    FixtureEmbedder,
    export_index,
    fixture_present,
    load_fixture,
    smoke_items,
    write_queries,
)
from lexai.evaluation.judge import Judge
from lexai.evaluation.runner import RunResult, describe_settings, run_answers, run_retrieval
from lexai.evaluation.sources import IndexTextSource
from lexai.query.condense import condense
from lexai.services.sessions import SessionStore

DATASET = REPO_ROOT / "eval" / "datasets" / "golden_v1.jsonl"
FIXTURE_DIR = REPO_ROOT / "eval" / "fixtures"
BASELINE = REPO_ROOT / "eval" / "baselines" / "main.json"
GATE_METRICS = ("recall@8", "mrr@10")
GATE_TOLERANCE = 0.02

CONFIGS = {
    "hybrid": "dense + BM25 (RRF) with Phase 3 routing (default)",
    "dense": "dense only, with routing",
    "bm25": "BM25 only, with routing",
    "no-routing": "hybrid without exact lookups / counterparts / code preference (Phase 2 behaviour)",
    "hyde": "hybrid + HyDE passage (one extra model call per question)",
    "rerank": "hybrid + cross-encoder reranker (needs requirements-rerank.txt)",
}


def commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git on PATH is expected
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        return out.stdout.strip() or "local"
    except OSError:
        return "local"


def reports_dir() -> Path:
    path = REPO_ROOT / "eval" / "reports" / commit()
    path.mkdir(parents=True, exist_ok=True)
    return path


def apply_config(settings: Settings, name: str) -> Settings:
    if name not in CONFIGS:
        raise SystemExit(f"unknown config {name!r}; choose from {', '.join(CONFIGS)}")
    if name in ("dense", "bm25"):
        settings.retrieval.mode = name  # type: ignore[assignment]
    elif name == "no-routing":
        settings.query.routing_enabled = False
    elif name == "hyde":
        settings.retrieval.hyde_enabled = True
    elif name == "rerank":
        settings.rerank.enabled = True
    settings.cache.enabled = False  # every item is answered, never replayed
    return settings


def save(run: RunResult, name: str, settings: Settings, extra: dict[str, Any] | None = None) -> Path:
    path = reports_dir() / f"{name}.json"
    payload = {
        "config": run.config,
        "description": CONFIGS.get(run.config, run.config),
        "split": run.split,
        "commit": commit(),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": describe_settings(settings),
        "seconds": run.seconds,
        "summary": run.summary,
        "rows": [r.row() for r in run.results],
        **(extra or {}),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


async def valid_items(
    container: Container, items: list[GoldenItem]
) -> tuple[list[GoldenItem], list[dict[str, Any]]]:
    assert container.store is not None and container.collection is not None, "no live index"
    source = IndexTextSource(container.store, container.collection)
    good, bad = [], []
    for item in items:
        v = await validate_item(item, source)
        if v.ok:
            good.append(item)
        else:
            bad.append({"id": item.id, "category": item.category, "problems": v.problems})
    return good, bad


# ---------------------------------------------------------------- commands


async def cmd_validate(args: argparse.Namespace) -> int:
    container = await Container.build(get_settings())
    try:
        items = load_items(Path(args.dataset))
        good, bad = await valid_items(container, items)
    finally:
        await container.aclose()
    (reports_dir() / "validation.json").write_text(json.dumps({"valid": len(good), "invalid": bad}, indent=2))
    print(f"{len(good)}/{len(items)} items valid")
    for b in bad:
        print(f"  INVALID {b['id']}: {'; '.join(b['problems'])}")
    return 1 if bad else 0


async def cmd_retrieval(args: argparse.Namespace) -> int:
    items = load_dataset(Path(args.dataset), args.split)
    for name in args.configs.split(","):
        settings = apply_config(Settings(), name)
        try:
            container = await Container.build(settings)
        except Exception as exc:  # e.g. the reranker extra is not installed
            print(f"[skip] {name}: {exc}")
            continue
        try:
            good, bad = await valid_items(container, items)
            run = await run_retrieval(container.chat, container.sessions, good, name, args.split)
        finally:
            await container.aclose()
        path = save(run, f"retrieval-{args.split}-{name}", settings, {"excluded": bad})
        o = run.summary["overall"]
        print(
            f"{name:11} n={run.summary['scored']:3}  hit@1 {o['hit@1']:.3f}  hit@5 {o['hit@5']:.3f}  "
            f"recall@8 {o['recall@8']:.3f}  mrr@10 {o['mrr@10']:.3f}  ndcg@10 {o['ndcg@10']:.3f}  "
            f"({run.seconds:.0f}s) -> {path.name}",
            flush=True,
        )
    return 0


async def cmd_answers(args: argparse.Namespace) -> int:
    settings = apply_config(Settings(), "hybrid")
    container = await Container.build(settings)
    try:
        items = load_dataset(Path(args.dataset), args.split)
        good, bad = await valid_items(container, items)
        if args.limit:
            good = stratified(good, args.limit)
        assert container.store is not None and container.collection is not None
        source = IndexTextSource(container.store, container.collection)

        async def passage_text(citation: dict[str, Any]) -> str:
            key = citation["doc_id"] if citation.get("law_type") == "judgment" else citation["label"]
            return await source(key) or citation.get("snippet", "")

        judge = Judge.from_env(passage_text)
        print(
            f"answering {len(good)} items; judge: {judge.name if judge else 'none (judged metrics not run)'}"
        )
        checkpoint = reports_dir() / f"answers-{args.split}.partial.jsonl"
        run = await run_answers(
            container.chat,
            container.sessions,
            good,
            args.split,
            judge,
            checkpoint=checkpoint,
            settings={
                **describe_settings(settings),
                "limit": args.limit,
                "judge": judge.name if judge else None,
            },
        )
    finally:
        await container.aclose()
    path = save(run, f"answers-{args.split}", settings, {"excluded": bad, "limit": args.limit})
    checkpoint.unlink(missing_ok=True)
    print(json.dumps(run.summary["overall"], indent=2))
    print(f"-> {path}")
    return 0


def stratified(items: list[GoldenItem], limit: int) -> list[GoldenItem]:
    """Up to `limit` items, spread across categories in round-robin order."""
    by_cat: dict[str, list[GoldenItem]] = {}
    for i in sorted(items, key=lambda i: i.id):
        by_cat.setdefault(i.category, []).append(i)
    picked: list[GoldenItem] = []
    while len(picked) < limit and any(by_cat.values()):
        for cat in sorted(by_cat):
            if by_cat[cat] and len(picked) < limit:
                picked.append(by_cat[cat].pop(0))
    return picked


async def cmd_export_fixture(args: argparse.Namespace) -> int:
    settings = get_settings()
    container = await Container.build(settings)
    try:
        assert container.store and container.collection and container.meta, "no live index"
        out = Path(args.fixture_dir)
        n = await export_index(container.store, container.collection, container.meta, out)
        print(f"exported {n} chunks")
        items, _bad = await valid_items(container, smoke_items(load_dataset(Path(args.dataset))))
        queries: dict[str, dict[str, Any]] = {}
        for item in items:
            standalone = item.question
            if item.turns:
                history = SessionStore(30, 20, 10)
                sid = history.create()
                for t in item.turns:
                    history.append_turn(sid, t, "(previous answer)")
                standalone = await condense(container.llm, item.question, history.history(sid))
            vector = await container.embedder.embed_query(standalone)
            queries[item.id] = {"standalone": standalone, "vector": [round(v, 6) for v in vector]}
        write_queries(out, queries)
        print(f"exported {len(queries)} smoke queries to {out}")
    finally:
        await container.aclose()
    return 0


async def offline_run(
    config: str = "hybrid", fixture_dir: Path = FIXTURE_DIR, dataset: Path = DATASET
) -> RunResult:
    """Retrieval over the exported smoke fixture: no model server, no index on disk."""
    store, collection, _meta, queries = await load_fixture(fixture_dir)
    settings = apply_config(Settings(), config)
    settings.query.condense_enabled = False  # follow-ups use the rewrite stored in the fixture
    embedder = FixtureEmbedder(queries)
    sessions = SessionStore(30, 20, 10)
    chat = await build_chat(settings, None, embedder, None, store, collection, sessions, None)  # type: ignore[arg-type]
    items = [i for i in load_dataset(dataset) if i.id in queries]
    swapped = [i.model_copy(update={"question": queries[i.id]["standalone"], "turns": []}) for i in items]
    run = await run_retrieval(chat, sessions, swapped, config, "smoke")
    await store.close()
    return run


def gate(summary: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    failures = []
    for metric in GATE_METRICS:
        now, base = summary["overall"][metric], baseline["overall"][metric]
        if now < base - GATE_TOLERANCE:
            failures.append(f"{metric} {now:.3f} < baseline {base:.3f} - {GATE_TOLERANCE}")
    return failures


def cmd_offline(args: argparse.Namespace) -> int:
    fixture_dir, baseline = Path(args.fixture_dir), Path(args.baseline)
    if not fixture_present(fixture_dir):
        print(
            "no fixture in eval/fixtures: run `python -m lexai.evaluation export-fixture` where the index is"
        )
        return 1
    run = asyncio.run(offline_run(args.config, fixture_dir, Path(args.dataset)))
    o = run.summary["overall"]
    print(
        f"offline smoke ({run.summary['scored']} items, {args.config}): "
        + "  ".join(f"{k} {v:.3f}" for k, v in o.items())
    )
    if args.write_baseline:
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(
            json.dumps(
                {"commit": commit(), "config": args.config, "overall": o, "items": run.summary["scored"]},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"baseline written: {baseline}")
        return 0
    if not baseline.exists():
        print("no baseline: run with --write-baseline on main")
        return 1
    failures = gate(run.summary, json.loads(baseline.read_text(encoding="utf-8")))
    for f in failures:
        print(f"GATE FAILED: {f}")
    print("GATE PASSED" if not failures else "")
    return 1 if failures else 0


def cmd_report(_: argparse.Namespace) -> int:
    path = report.write(reports_dir())
    print(f"report: {path}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    items = load_items(Path(args.dataset))
    write_lock(Path(args.dataset), items)
    print(f"locked {sum(i.split == 'test' for i in items)} test items")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m lexai.evaluation")
    parser.add_argument("--dataset", default=str(DATASET))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p = sub.add_parser("retrieval")
    p.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    p.add_argument("--configs", default="hybrid")
    p = sub.add_parser("answers")
    p.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    p.add_argument("--limit", type=int, default=0, help="stratified subset size (0 = all)")
    p = sub.add_parser("export-fixture")
    p.add_argument("--fixture-dir", default=str(FIXTURE_DIR))
    p = sub.add_parser("offline")
    p.add_argument("--fixture-dir", default=str(FIXTURE_DIR))
    p.add_argument("--baseline", default=str(BASELINE))
    p.add_argument("--config", default="hybrid", choices=list(CONFIGS))
    p.add_argument("--write-baseline", action="store_true")
    sub.add_parser("report")
    sub.add_parser("lock")
    args = parser.parse_args(argv)
    handlers = {
        "validate": cmd_validate,
        "retrieval": cmd_retrieval,
        "answers": cmd_answers,
        "export-fixture": cmd_export_fixture,
    }
    if args.cmd in handlers:
        sys.exit(asyncio.run(handlers[args.cmd](args)))
    sys.exit({"report": cmd_report, "lock": cmd_lock, "offline": cmd_offline}[args.cmd](args))


if __name__ == "__main__":
    main()
