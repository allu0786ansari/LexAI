"""
`lexai-ingest`: build the search index from the PDFs in the data directory.

The new index is built in a temporary directory, loaded back and checked, and
only then swapped in for the live one (the previous index is kept as a
backup). A failed run never leaves the API without an index.

Usage:
    lexai-ingest                      # all PDFs
    lexai-ingest --only "IPC*"        # a subset, for a quick smoke run
    lexai-ingest --dry-run            # parse and chunk only; no embedding calls
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import shutil
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lexai.clients.embedder import EmbeddingClient
from lexai.core.errors import LexAIError
from lexai.core.logging import configure_logging, get_logger
from lexai.core.settings import Settings, get_settings
from lexai.domain.models import Chunk
from lexai.ingestion.loader import chunk_document
from lexai.retrieval.index import LocalIndex

logger = get_logger(__name__)
PROGRESS_EVERY = 256


def select_pdfs(data_dir: Path, patterns: list[str]) -> list[Path]:
    pdfs = sorted(p for p in data_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if patterns:
        pdfs = [p for p in pdfs if any(fnmatch.fnmatch(p.name.lower(), pat.lower()) for pat in patterns)]
    return pdfs


async def embed_with_progress(embedder: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    started = time.perf_counter()
    for start in range(0, len(texts), PROGRESS_EVERY):
        vectors.extend(await embedder.embed_documents(texts[start : start + PROGRESS_EVERY]))
        logger.info(
            "embedding_progress",
            done=len(vectors),
            total=len(texts),
            seconds=round(time.perf_counter() - started, 1),
        )
    return vectors


def swap_in(new_dir: Path, live_dir: Path) -> Path | None:
    """Move the live index aside (one backup kept) and the new one into place."""
    backup: Path | None = None
    if live_dir.exists():
        backup = live_dir.with_name(live_dir.name + ".backup")
        if backup.exists():
            shutil.rmtree(backup)
        live_dir.rename(backup)
    new_dir.rename(live_dir)
    return backup


async def run(
    settings: Settings, data_dir: Path, index_dir: Path, only: list[str], dry_run: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    if not await asyncio.to_thread(data_dir.is_dir):
        raise SystemExit(f"Data directory not found: {data_dir}")
    pdfs = await asyncio.to_thread(select_pdfs, data_dir, only)
    if not pdfs:
        raise SystemExit(f"No PDFs matched in {data_dir}")
    logger.info("ingestion_started", files=len(pdfs), dry_run=dry_run, embed_model=settings.embed.model)

    all_chunks: list[Chunk] = []
    per_file: list[dict[str, Any]] = []
    for pdf in pdfs:
        chunks, pages = await asyncio.to_thread(
            chunk_document, pdf, settings.retrieval.chunk_size_chars, settings.retrieval.chunk_overlap_chars
        )
        all_chunks.extend(chunks)
        law_type = chunks[0].metadata.law_type if chunks else "n/a"
        per_file.append({"file": pdf.name, "pages": pages, "chunks": len(chunks), "law_type": law_type})
        if not chunks:
            logger.warning("file_yielded_no_text", file=pdf.name)

    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "embedding_model": settings.embed.model,
        "files": len(pdfs),
        "chunks": len(all_chunks),
        "law_types": dict(Counter(c.metadata.law_type for c in all_chunks)),
        "per_file": per_file,
    }
    if not all_chunks:
        raise SystemExit("No text extracted from any PDF; nothing to index.")
    if dry_run:
        report["seconds"] = round(time.perf_counter() - started, 1)
        return report

    embedder = EmbeddingClient(settings.embed)
    try:
        vectors = await embed_with_progress(embedder, [c.text for c in all_chunks])
    finally:
        await embedder.aclose()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    building = index_dir.with_name(f".{index_dir.name}.building-{stamp}")
    try:
        index = LocalIndex.build(all_chunks, vectors, settings.embed.model)
        index.save(building)
        # Load it back exactly as the API would before making it live.
        LocalIndex.load(
            building, expected_model=settings.embed.model, expected_dimension=settings.embed.dimension
        )
        report["seconds"] = round(time.perf_counter() - started, 1)
        (building / "ingestion_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        backup = swap_in(building, index_dir)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    report["index_dir"] = str(index_dir)
    report["previous_index_backup"] = str(backup) if backup else None
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the LexAI search index from PDFs.")
    parser.add_argument("--data-dir", type=Path, help="Defaults to LEXAI_RETRIEVAL_DATA_DIR")
    parser.add_argument("--index-dir", type=Path, help="Defaults to LEXAI_RETRIEVAL_INDEX_DIR")
    parser.add_argument("--only", action="append", default=[], help="Filename glob; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="Parse and chunk only; no embedding calls")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.api)
    try:
        report = asyncio.run(
            run(
                settings,
                (args.data_dir or settings.retrieval.data_dir).resolve(),
                (args.index_dir or settings.retrieval.index_dir).resolve(),
                args.only,
                args.dry_run,
            )
        )
    except LexAIError as exc:
        logger.error("ingestion_failed", code=exc.code, error=str(exc))
        sys.exit(1)
    summary = {k: v for k, v in report.items() if k != "per_file"}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
