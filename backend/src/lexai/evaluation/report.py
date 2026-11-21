"""Build eval/reports/<commit>/report.md from the JSON results in that directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RETRIEVAL_COLS = ("hit@1", "hit@5", "recall@8", "mrr@10", "ndcg@10")
ANSWER_COLS = (
    ("answered", "answered"),
    ("verified", "verified (no flags)"),
    ("coverage", "citation coverage"),
    ("citation_validity", "citation validity"),
    ("gold_cited", "gold provision cited"),
    ("must_mention", "key phrases present"),
    ("code_correct", "correct code applied"),
    ("must_not_mention_ok", "injection resisted"),
    ("faithfulness", "faithfulness (judge)"),
    ("answer_relevancy", "answer relevancy (judge)"),
    ("context_precision", "context precision (judge)"),
    ("context_recall", "context recall (judge)"),
    ("citation_support", "citation support (judge)"),
)


def _f(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return "-" if v != v else f"{v:.3f}"  # NaN
    return str(v)


def _load(directory: Path, prefix: str) -> list[dict[str, Any]]:
    out = []
    for path in sorted(directory.glob(f"{prefix}*.json")):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def write(directory: Path) -> Path:
    lines: list[str] = []
    order = ["hybrid", "dense", "bm25", "no-routing", "hyde", "rerank"]
    retrieval = sorted(
        _load(directory, "retrieval-"),
        key=lambda r: (r["split"], order.index(r["config"]) if r["config"] in order else 99),
    )
    answers = _load(directory, "answers-")
    validation = _load(directory, "validation")
    base = [r for r in retrieval if r["config"] == "hybrid"]
    first = (answers or base or retrieval or [{}])[0]
    lines += [
        f"# LexAI evaluation — commit `{first.get('commit', directory.name)}`",
        "",
        f"Date: {first.get('date', '?')}  ",
        "Dataset: `eval/datasets/golden_v1.jsonl`; every item is checked against the indexed "
        "source text before it is scored (items that fail are excluded and listed below).  ",
        f"Base settings: `{json.dumps(first.get('settings', {}))}`",
        "",
    ]
    if validation:
        v = validation[0]
        lines += [f"Validation: {v['valid']} items valid, {len(v['invalid'])} excluded.", ""]
        for bad in v["invalid"]:
            lines.append(f"- excluded `{bad['id']}`: {'; '.join(bad['problems'])}")
        lines.append("")

    if retrieval:
        lines += ["## Retrieval (provision-level, deterministic)", ""]
        lines.append("| Config | Split | Items | " + " | ".join(RETRIEVAL_COLS) + " | Time |")
        lines.append("|" + " --- |" * (len(RETRIEVAL_COLS) + 4))
        for r in retrieval:
            o = r["summary"]["overall"]
            lines.append(
                f"| {r['config']} | {r['split']} | {r['summary']['scored']} | "
                + " | ".join(_f(o.get(c)) for c in RETRIEVAL_COLS)
                + f" | {r['seconds']:.0f}s |"
            )
        lines += [
            "",
            "Configs: " + "; ".join(f"**{r['config']}** — {r['description']}" for r in retrieval),
            "",
        ]
        for r in retrieval:
            if r["config"] != "hybrid":
                continue
            lines += [f"### By category ({r['split']}, hybrid)", ""]
            lines.append("| Category | n | " + " | ".join(RETRIEVAL_COLS) + " |")
            lines.append("|" + " --- |" * (len(RETRIEVAL_COLS) + 2))
            for cat, m in r["summary"]["by_category"].items():
                lines.append(
                    f"| {cat} | {m['n']} | " + " | ".join(_f(m.get(c)) for c in RETRIEVAL_COLS) + " |"
                )
            s = r["summary"]
            a = s["abstention"]
            lines += [
                "",
                f"Abstention before generation (threshold {r['settings'].get('abstain_min_dense')}): "
                f"precision {_f(a['abstention_precision'])}, recall {_f(a['abstention_recall'])}, "
                f"false abstentions {int(a['false_abstentions'])} of {a['answerable_items']} answerable.  ",
                f"Refusal of crime-assistance requests: recall {_f(s['refusal_recall'])}, "
                f"false refusals {s['refusal_false_positives']}.  ",
                f"Applicable code chosen correctly: {_f(s['code_correct'])}.",
                "",
            ]
            misses = [row for row in r["rows"] if row.get("hit@5") == 0.0]
            if misses:
                lines += ["Misses (no gold in the top 5):", ""]
                for row in misses[:25]:
                    lines.append(
                        f"- `{row['id']}` {row['question']} — gold {row['gold']}; got {row['top'][:5]}"
                    )
                lines.append("")

    for a in answers:
        o = a["summary"]["overall"]
        judge = a["summary"].get("judge") or "none"
        lines += [
            f"## Answers ({a['split']}, {a['summary']['items']} items, judge: {judge})",
            "",
            "| Metric | Value | n |",
            "| --- | --- | --- |",
        ]
        for key, label in ANSWER_COLS:
            if key in o:
                lines.append(f"| {label} | {_f(o[key])} | {int(o.get(key + '_n', 0))} |")
        ab = a["summary"]["abstention"]
        lines += [
            f"| abstention precision | {_f(ab['abstention_precision'])} | |",
            f"| abstention recall | {_f(ab['abstention_recall'])} | |",
            f"| time to first token (ms, mean) | {_f(o.get('ttft_ms'))} | |",
            f"| total time (ms, mean) | {_f(o.get('total_ms'))} | |",
            f"| prompt processing (tokens/s, mean) | {_f(o.get('prompt_tok_s'))} | |",
            f"| generation (tokens/s, mean) | {_f(o.get('decode_tok_s'))} | |",
            "",
            f"Errors: {a['summary']['errors']}.",
            "",
        ]
        if not a["summary"].get("judge"):
            lines += [
                "Judged metrics (faithfulness, relevancy, context precision/recall, citation support) "
                "were not run: no judge model is configured. They need a model larger than the "
                "7B generator "
                "(LEXAI_EVAL_JUDGE_BASE_URL / _MODEL / _API_KEY).",
                "",
            ]
    load = directory / "load.json"
    if load.exists():
        data = json.loads(load.read_text(encoding="utf-8"))
        lines += [
            "## Load (laptop, one process, embedded Qdrant)",
            "",
            "| Run | Requests | Errors | p50 ms | p95 ms | max ms | req/s |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        rows = list(data.get("search", []))
        for a in data.get("answers", []):
            rows += [a, a["ttft"]]
        for r in rows:
            lines.append(
                f"| {r['name']} | {r['requests']} | {r['errors']} | {r['p50_ms']} | {r['p95_ms']} | "
                f"{r['max_ms']} | {r['throughput_rps']} |"
            )
        lines.append("")
    path = directory / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
