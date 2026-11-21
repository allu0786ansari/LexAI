"""
`lexai-ingest` / `python -m lexai.ingestion.cli`: manage the corpus and the search index.

    fetch   download the manifest's sources into data/raw and pin their checksums
    build   parse, chunk, embed and publish a new index (only if all checks pass)
    status  show the live index and what it contains
    backup  snapshot the live index into a backup directory (Qdrant server only)
    restore restore a backup and make it live

Examples:
    python -m lexai.ingestion.cli fetch
    python -m lexai.ingestion.cli build --dry-run        # parse + checks only, no model calls
    python -m lexai.ingestion.cli build --only ipc-1860 --only "sc-*"
    python -m lexai.ingestion.cli status
    python -m lexai.ingestion.cli backup --dir /backups --keep 7
    python -m lexai.ingestion.cli restore --dir /backups latest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from lexai.core.errors import LexAIError
from lexai.core.logging import configure_logging, get_logger
from lexai.core.settings import Settings, get_settings
from lexai.ingestion import backup as backups
from lexai.ingestion.fetch import fetch_documents, format_results
from lexai.ingestion.indexer import BuildReport, build_index, select_docs, write_report
from lexai.ingestion.manifest import expand, load_manifest
from lexai.retrieval.qdrant_store import QdrantStore

logger = get_logger(__name__)


def _print_build(report: BuildReport) -> None:
    print(f"\n{'document':34} {'type':12} {'pages':>5} {'units':>5} {'chunks':>6}  notes")
    print("-" * 100)
    for d in report.documents:
        notes = "; ".join(d["problems"] + d["warnings"])
        print(f"{d['id']:34} {d['type']:12} {d['pages']:>5} {d['units']:>5} {d['chunks']:>6}  {notes}")
    print("-" * 100)
    for c in report.checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['check']} -> {c['detail']}")
    for p in report.problems:
        print(f"[PROBLEM] {p}")
    print(
        f"\nchunks={report.chunks} embedded_docs={report.embedded_docs} copied_docs={report.copied_docs} "
        f"seconds={report.seconds}"
    )
    print(f"BUILD STATUS: {report.status}")


async def _status(settings: Settings) -> int:
    store = QdrantStore.connect(settings.qdrant)
    try:
        live = await store.live_collection()
        builds = await store.build_collections()
        print(f"Qdrant: {settings.qdrant.url or settings.qdrant.path}")
        print(f"builds: {builds}")
        if not live:
            print("live index: none (run build)")
            return 1
        meta = await store.read_meta(live)
        count = await store.count(live)
        print(f"live index: {live} ({count} chunks)")
        if meta:
            print(json.dumps(meta.model_dump(exclude={"docs"}), indent=2))
            print(f"documents: {len(meta.docs)}")
            for doc_id, info in sorted(meta.docs.items()):
                print(f"  {doc_id:34} {info.chunks:5}  {info.title}")
        return 0
    finally:
        await store.close()


async def _backup(settings: Settings, root: Path, keep: int) -> int:
    store = QdrantStore.connect(settings.qdrant)
    try:
        info = await backups.backup(store, root, keep=keep)
    finally:
        await store.close()
    print(f"backup: {info.path} ({info.collection}, {info.points} points, {info.size_bytes / 1e6:.1f} MB)")
    if info.pruned:
        print(f"removed old backups: {', '.join(info.pruned)}")
    return 0


async def _restore(settings: Settings, root: Path, which: str) -> int:
    path = backups.resolve(root, which)
    store = QdrantStore.connect(settings.qdrant)
    try:
        result = await backups.restore(store, path)
    finally:
        await store.close()
    print(
        f"restored {path.name}: {result['collection']} ({result['points']} points, backed up "
        f"{result['created']}) is live; previously live: {result['previous'] or 'none'}"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LexAI corpus and index management.")
    sub = parser.add_subparsers(dest="command")
    p_fetch = sub.add_parser("fetch", help="download and pin sources")
    p_fetch.add_argument("--only", action="append", default=[], help="document id (repeatable, * suffix ok)")
    p_fetch.add_argument("--update", action="store_true", help="accept a changed upstream file and re-pin it")
    p_build = sub.add_parser("build", help="build and publish a new index")
    p_build.add_argument("--only", action="append", default=[], help="document id (repeatable, * suffix ok)")
    p_build.add_argument(
        "--dry-run", action="store_true", help="parse and check only; no model or Qdrant calls"
    )
    sub.add_parser("status", help="show the live index")
    p_backup = sub.add_parser("backup", help="snapshot the live index (Qdrant server only)")
    p_backup.add_argument("--dir", type=Path, default=Path("/backups"), help="backup root directory")
    p_backup.add_argument("--keep", type=int, default=7, help="newest backups to keep")
    p_restore = sub.add_parser("restore", help="restore a backup and make it live")
    p_restore.add_argument("--dir", type=Path, default=Path("/backups"), help="backup root directory")
    p_restore.add_argument(
        "backup", nargs="?", default="latest", help="backup name or path (default: latest)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.api)
    command = args.command or "build"
    try:
        if command == "fetch":
            docs = select_docs(
                expand(load_manifest(settings.corpus.manifest), settings.corpus.bundled_dir), args.only
            )
            results = asyncio.run(fetch_documents(docs, settings.corpus, update=args.update))
            print(format_results(results))
            sys.exit(1 if any(r.status == "failed" for r in results) else 0)
        elif command == "status":
            sys.exit(asyncio.run(_status(settings)))
        elif command == "backup":
            sys.exit(asyncio.run(_backup(settings, args.dir, args.keep)))
        elif command == "restore":
            sys.exit(asyncio.run(_restore(settings, args.dir, args.backup)))
        else:
            only = getattr(args, "only", [])
            dry = getattr(args, "dry_run", False)
            report = asyncio.run(build_index(settings, only=only, dry_run=dry))
            path = write_report(report, settings.corpus.report_dir)
            _print_build(report)
            print(f"report: {path}")
            sys.exit(0 if report.status.startswith(("live", "dry run ok")) else 1)
    except LexAIError as exc:
        logger.error("ingest_failed", code=exc.code, error=str(exc))
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
