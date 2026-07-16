"""
Main ingestion pipeline entry point.

Rebuilds the FAISS dense index and BM25 sparse index from every PDF in
Data/, with semantic chunking and per-chunk legal metadata.

Usage:
    python -m ingestion.ingest
    python -m ingestion.ingest --dry-run
    python -m ingestion.ingest --data-dir /path/to/pdfs --no-backup

Design notes:
  - Idempotent by default: an existing Database/ is moved to a timestamped
    backup before a fresh one is written, so a bad run never silently
    destroys a working index. Use --no-backup to skip this.
  - --dry-run validates PDF loading, metadata tagging, and stats output
    without calling the embedding API — it swaps semantic chunking for a
    plain recursive splitter, since semantic chunking itself requires
    embedding calls to detect breakpoints. Useful for CI / no-API-key
    smoke tests; NOT a substitute for a real ingestion run.
  - Embedding calls are batched and retried with exponential backoff on
    transient API errors (rate limits, 5xx) — a 70-document corpus makes
    enough calls that a single transient failure should not fail the
    whole run.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.config.logging import configure_logging, get_logger
from app.config.settings import Settings, get_settings
from app.services.bm25_store import Bm25Store
from app.services.vector_store import FaissVectorStore
from ingestion.chunker import SemanticChunker, SemanticChunkerConfig
from ingestion.metadata import build_document_metadata, tag_chunk_metadata

MIN_PAGE_CHARS = 10  # pages with less text than this are treated as blank/scan noise and skipped

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# PDF loading
# ----------------------------------------------------------------------

def load_pdf_pages(pdf_path: Path) -> list[Document]:
    """
    Load a PDF into one Document per page, preserving 1-indexed page numbers
    in metadata. Skips near-empty pages (scanned images with no OCR layer,
    blank separator pages) rather than feeding empty chunks downstream.
    """
    try:
        reader = PdfReader(str(pdf_path))
    except PdfReadError as exc:
        logger.error("pdf_read_failed", file=pdf_path.name, error=str(exc))
        return []

    pages: list[Document] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pypdf can raise a variety of parser errors on malformed pages
            logger.warning("page_extract_failed", file=pdf_path.name, page=page_number, error=str(exc))
            continue
        if len(text.strip()) < MIN_PAGE_CHARS:
            continue
        pages.append(
            Document(page_content=text, metadata={"source_file": pdf_path.name, "page": page_number})
        )

    if not pages:
        logger.warning("pdf_yielded_no_pages", file=pdf_path.name)
    return pages


# ----------------------------------------------------------------------
# Per-document chunking + metadata tagging
# ----------------------------------------------------------------------

def process_document(
    pdf_path: Path,
    chunker: SemanticChunker | None,
    dry_run_splitter: RecursiveCharacterTextSplitter | None,
) -> tuple[list[Document], int]:
    """Returns (tagged chunks, page count) for a single source PDF."""
    pages = load_pdf_pages(pdf_path)
    if not pages:
        return [], 0

    doc_meta = build_document_metadata(pdf_path)

    if chunker is not None:
        raw_chunks = chunker.split_documents(pages)
    else:
        assert dry_run_splitter is not None
        raw_chunks = []
        for page in pages:
            for piece in dry_run_splitter.split_text(page.page_content):
                if piece.strip():
                    raw_chunks.append(Document(page_content=piece, metadata=dict(page.metadata)))

    tagged: list[Document] = []
    for i, chunk in enumerate(raw_chunks):
        meta = tag_chunk_metadata(
            chunk_text=chunk.page_content,
            doc_metadata=doc_meta,
            page_number=chunk.metadata.get("page"),
            chunk_index=i,
        )
        tagged.append(Document(page_content=chunk.page_content.strip(), metadata=meta))

    return tagged, len(pages)


# ----------------------------------------------------------------------
# Embedding with retry
# ----------------------------------------------------------------------

def _is_retryable_error(exc: BaseException) -> bool:
    """Retry on transient errors that are likely to clear after a short wait."""
    try:
        from google.genai.errors import APIError
    except ImportError:
        APIError = ()  # pragma: no cover - SDK always present in this project
    message = str(exc).lower()
    return isinstance(exc, (APIError, TimeoutError, ConnectionError)) or "resource_exhausted" in message or "429" in message


def _is_quota_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "resource_exhausted" in message or "429" in message or "quota" in message


def _extract_retry_delay_seconds(exc: BaseException, settings: Settings) -> int:
    message = str(exc)
    match = re.search(r"retry in\s+([0-9.]+)s", message, re.IGNORECASE)
    if match:
        return max(settings.embedding_quota_wait_seconds, math.ceil(float(match.group(1))))
    return settings.embedding_quota_wait_seconds if _is_quota_error(exc) else int(settings.embedding_retry_min_seconds)


def build_retrying_embed_fn(settings: Settings):
    """
    Retry embedding calls with a quota-aware pause. Google free-tier
    embedding requests commonly reject immediate retries with a 429 until
    the quota window resets, so the next attempt should wait at least one minute.
    """

    def embed(client, texts: list[str]) -> list[list[float]]:
        last_exc: BaseException | None = None
        for attempt in range(1, settings.embedding_max_retries + 1):
            try:
                return client.embed_documents(texts)
            except Exception as exc:  # pragma: no cover - exercised via runtime integration
                last_exc = exc
                if not _is_retryable_error(exc):
                    raise
                wait_seconds = _extract_retry_delay_seconds(exc, settings)
                if attempt < settings.embedding_max_retries:
                    logger.warning(
                        "embedding_batch_retry",
                        attempt=attempt,
                        wait_seconds=wait_seconds,
                        error=str(exc),
                    )
                    time.sleep(wait_seconds)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Embedding retries exhausted without a captured error.")

    return embed


def batched(items: list, batch_size: int):
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def select_pdf_files_to_process(data_dir: Path, checkpoint_path: Path | None = None) -> list[Path]:
    pdf_files = sorted(p for p in data_dir.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
    if checkpoint_path is None or not checkpoint_path.exists():
        return pdf_files

    with open(checkpoint_path, "r", encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)

    completed = set(payload.get("completed_files", []))
    return [pdf_path for pdf_path in pdf_files if pdf_path.name not in completed]


def embed_all(chunks: list[Document], settings: Settings) -> list[list[float]]:
    from app.services.providers import build_embeddings_client

    client = build_embeddings_client(settings, task_type=settings.embedding_task_type)
    embed_fn = build_retrying_embed_fn(settings)

    vectors: list[list[float]] = []
    texts = [c.page_content for c in chunks]
    total_batches = (len(texts) + settings.embedding_batch_size - 1) // settings.embedding_batch_size
    for batch_num, batch in enumerate(batched(texts, settings.embedding_batch_size), start=1):
        t0 = time.time()
        batch_vectors = embed_fn(client, batch)
        vectors.extend(batch_vectors)
        logger.info(
            "embedding_batch_complete",
            batch=batch_num,
            of=total_batches,
            size=len(batch),
            seconds=round(time.time() - t0, 2),
        )
    return vectors


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def backup_existing_database(database_dir: Path) -> Path | None:
    if not database_dir.exists() or not any(database_dir.iterdir()):
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = database_dir.parent / f"{database_dir.name}_backup_{timestamp}"
    shutil.move(str(database_dir), str(backup_dir))
    return backup_dir


def run_ingestion(
    data_dir: Path,
    database_dir: Path,
    dry_run: bool,
    no_backup: bool,
    settings: Settings,
) -> dict:
    run_start = time.time()

    if not data_dir.exists() or not data_dir.is_dir():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    pdf_files = sorted(
        p for p in data_dir.iterdir() if p.suffix.lower() == ".pdf" and p.is_file()
    )
    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in {data_dir}")

    logger.info("ingestion_started", data_dir=str(data_dir), num_source_files=len(pdf_files), dry_run=dry_run)

    if not dry_run and getattr(settings, "embedding_provider", "ollama") == "google":
        settings.require_google_api_key()

    chunker: SemanticChunker | None = None
    dry_run_splitter: RecursiveCharacterTextSplitter | None = None
    if dry_run:
        dry_run_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_max_chars,
            chunk_overlap=settings.chunk_hard_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
    else:
        from app.services.providers import build_embeddings_client

        chunk_embeddings_client = build_embeddings_client(settings, task_type="semantic_similarity")
        chunker = SemanticChunker(
            embeddings=chunk_embeddings_client,
            config=SemanticChunkerConfig(
                buffer_size=settings.chunk_buffer_size,
                breakpoint_percentile=settings.chunk_breakpoint_percentile,
                min_chunk_chars=settings.chunk_min_chars,
                max_chunk_chars=settings.chunk_max_chars,
                hard_split_overlap=settings.chunk_hard_overlap,
            ),
        )

    all_chunks: list[Document] = []
    per_document_stats: list[dict] = []

    for pdf_path in pdf_files:
        doc_start = time.time()
        chunks, num_pages = process_document(pdf_path, chunker, dry_run_splitter)
        all_chunks.extend(chunks)
        per_document_stats.append(
            {
                "file": pdf_path.name,
                "pages": num_pages,
                "chunks": len(chunks),
                "seconds": round(time.time() - doc_start, 2),
            }
        )
        logger.info(
            "document_processed",
            file=pdf_path.name,
            pages=num_pages,
            chunks=len(chunks),
        )

    if not all_chunks:
        raise RuntimeError("Ingestion produced zero chunks across all source documents — aborting before touching Database/.")

    backup_path = None
    if not no_backup:
        backup_path = backup_existing_database(database_dir)
        if backup_path:
            logger.info("existing_database_backed_up", backup_path=str(backup_path))

    embedding_seconds = 0.0
    if dry_run:
        logger.warning("dry_run_skipping_faiss_build", reason="no embedding calls made in dry-run mode")
    else:
        embed_start = time.time()
        try:
            vectors = embed_all(all_chunks, settings)
        except Exception as exc:
            if _is_quota_error(exc):
                logger.warning(
                    "embedding_quota_exhausted_skipping_faiss_build",
                    error=str(exc),
                    chunks=len(all_chunks),
                )
                vectors = []
            else:
                raise
        embedding_seconds = round(time.time() - embed_start, 2)
        if vectors:
            if len(vectors) != len(all_chunks):
                raise RuntimeError(
                    f"Embedding count ({len(vectors)}) does not match chunk count ({len(all_chunks)}) — aborting."
                )
            vector_store = FaissVectorStore(dimension=len(vectors[0]))
            vector_store.add_documents(all_chunks, vectors)
            vector_store.save_local(database_dir)
            logger.info("faiss_index_saved", directory=str(database_dir), vectors=len(vectors))
        else:
            logger.warning(
                "faiss_index_not_built_due_to_embedding_failure",
                directory=str(database_dir),
                chunks=len(all_chunks),
            )

    bm25_store = Bm25Store(all_chunks)
    bm25_store.save_local(database_dir)
    logger.info("bm25_index_saved", directory=str(database_dir), documents=len(all_chunks))

    law_type_counts = Counter(c.metadata.get("law_type", "other") for c in all_chunks)
    total_seconds = round(time.time() - run_start, 2)

    stats = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "data_dir": str(data_dir),
        "database_dir": str(database_dir),
        "backup_created": str(backup_path) if backup_path else None,
        "embedding_model": settings.embedding_model,
        "total_source_documents": len(pdf_files),
        "total_chunks": len(all_chunks),
        "law_type_distribution": dict(law_type_counts),
        "per_document_stats": per_document_stats,
        "embedding_seconds": embedding_seconds,
        "total_seconds": total_seconds,
    }

    stats_path = database_dir / "ingestion_stats.json"
    database_dir.mkdir(parents=True, exist_ok=True)
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    logger.info(
        "ingestion_complete",
        total_documents=len(pdf_files),
        total_chunks=len(all_chunks),
        total_seconds=total_seconds,
        stats_file=str(stats_path),
    )
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the LexAI FAISS + BM25 indexes from Data/.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override Settings.data_dir")
    parser.add_argument("--database-dir", type=Path, default=None, help="Override Settings.database_dir")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate loading/chunking/metadata/stats without calling the embedding API. Skips FAISS build.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Overwrite Database/ in place instead of backing it up first.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    settings = get_settings()
    configure_logging(settings)

    data_dir = args.data_dir or settings.data_dir
    database_dir = args.database_dir or settings.database_dir

    try:
        run_ingestion(
            data_dir=data_dir,
            database_dir=database_dir,
            dry_run=args.dry_run,
            no_backup=args.no_backup,
            settings=settings,
        )
    except Exception as exc:
        logger.error("ingestion_failed", error=str(exc), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
