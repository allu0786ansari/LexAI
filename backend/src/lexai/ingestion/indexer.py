"""
Build a new search index from the manifest and make it live only if it passes checks.

Steps:
1. Verify every source (bundled checksums, pinned downloads).
2. Parse and chunk each document; collect structural problems (missing
   expected text, too few sections). Any problem stops the build.
3. Create a new collection. Documents unchanged since the live build (same
   file checksum, chunker, embedding model and prefix) are copied over with
   their vectors; the rest are embedded.
4. Check the new collection: chunk counts per document, and that each
   document's expected phrase retrieves that document.
5. Point the alias at the new collection; prune old builds.

The live index is never modified: a failed build leaves it serving.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import models

from lexai.clients.embedder import EmbeddingClient
from lexai.core.errors import ConfigurationError
from lexai.core.logging import get_logger
from lexai.core.settings import Settings
from lexai.domain.models import Chunk
from lexai.ingestion.chunkers import ParsedDocument, build_chunks, chunker_fingerprint
from lexai.ingestion.manifest import (
    DocumentSpec,
    expand,
    load_manifest,
    read_bundled_sums,
    read_lock,
    verify_source,
)
from lexai.ingestion.parsing import load_document
from lexai.retrieval.qdrant_store import (
    BuildMeta,
    DocBuildInfo,
    QdrantStore,
    SearchFilter,
    new_collection_name,
)
from lexai.retrieval.sparse import SPARSE_VERSION, average_length, doc_vector, query_vector

logger = get_logger(__name__)
EMBED_SLICE = 256


@dataclass
class BuildReport:
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    dry_run: bool = False
    collection: str | None = None
    previous: str | None = None
    status: str = "running"
    documents: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    embedded_docs: int = 0
    copied_docs: int = 0
    chunks: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def select_docs(docs: list[DocumentSpec], only: list[str]) -> list[DocumentSpec]:
    if not only:
        return docs
    picked = [
        d
        for d in docs
        if d.id in only or any(d.id.startswith(o.rstrip("*")) for o in only if o.endswith("*"))
    ]
    if not picked:
        raise ConfigurationError(f"--only {only} matched no document ids")
    return picked


async def build_index(
    settings: Settings,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
    store: QdrantStore | None = None,
) -> BuildReport:
    """Build and publish a new index. `store`: an open store to use (the API's, for an
    in-process reindex: embedded Qdrant allows one client per process); it is not closed."""
    started = time.perf_counter()
    report = BuildReport(dry_run=dry_run)
    corpus = settings.corpus
    all_docs = expand(load_manifest(corpus.manifest), corpus.bundled_dir)
    docs = select_docs(all_docs, only or [])
    # A partial build (--only) rebuilds the selected documents and copies every other
    # manifest document from the live index unchanged; it never drops them.
    carried = [d.id for d in all_docs if d not in docs] if only else []
    sums, lock = read_bundled_sums(corpus.bundled_dir), read_lock(corpus.lock_file)
    fingerprint = chunker_fingerprint(corpus.max_chunk_chars, corpus.chunk_overlap_chars)

    # 1-2. verify and parse ---------------------------------------------------
    parsed: dict[str, ParsedDocument] = {}
    hashes: dict[str, str] = {}
    for spec in docs:
        try:
            path, digest = verify_source(spec, corpus.bundled_dir, corpus.raw_dir, sums, lock)
        except ConfigurationError as exc:
            report.problems.append(str(exc))
            continue
        hashes[spec.id] = digest
        doc = await asyncio.to_thread(load_document, path)
        pd = await asyncio.to_thread(
            build_chunks, spec, doc, digest, path.name, corpus.max_chunk_chars, corpus.chunk_overlap_chars
        )
        parsed[spec.id] = pd
        report.problems.extend(f"{spec.id}: {p}" for p in pd.problems)
        report.documents.append(
            {
                "id": spec.id,
                "title": pd.title,
                "type": spec.doc_type,
                "pages": pd.pages,
                "units": pd.units,
                "chunks": len(pd.chunks),
                "warnings": pd.warnings,
                "problems": pd.problems,
            }
        )
        logger.info("document_parsed", doc=spec.id, pages=pd.pages, units=pd.units, chunks=len(pd.chunks))

    report.chunks = sum(len(p.chunks) for p in parsed.values())
    if report.problems:
        report.status = "failed: source or parsing problems (live index unchanged)"
        report.seconds = round(time.perf_counter() - started, 1)
        return report
    if dry_run:
        report.status = "dry run ok"
        report.seconds = round(time.perf_counter() - started, 1)
        return report

    # 3. build a new collection -------------------------------------------------
    owns_store = store is None
    store = store or QdrantStore.connect(settings.qdrant)
    embedder = EmbeddingClient(settings.embed)
    name = new_collection_name()
    report.collection = name
    try:
        live = await store.live_collection()
        # Leftovers of interrupted builds (no metadata) waste space and, in embedded mode, memory.
        for leftover in await store.incomplete_builds(exclude=live):
            await store.delete_collection(leftover)
            logger.warning("removed_interrupted_build", collection=leftover)
        report.previous = live
        live_meta = await store.read_meta(live) if live else None
        reusable = (
            live_meta is not None
            and live_meta.embedding_model == settings.embed.model
            and live_meta.dimension == settings.embed.dimension
            and live_meta.document_prefix == settings.embed.document_prefix
            and live_meta.chunker_version == fingerprint
            and live_meta.sparse_version == SPARSE_VERSION
        )
        if carried and live and not reusable:
            raise ConfigurationError(
                "A partial rebuild (--only) needs the live index to be built with the current embedder, "
                "chunker and BM25 settings, to copy the other documents from. Run a full build."
            )
        if carried and not live:
            logger.info("first_build_is_partial", documents=[d.id for d in docs])
            carried = []  # nothing to carry over yet: this build indexes only the selected documents
        all_texts = [c.embed_text for p in parsed.values() for c in p.chunks]
        avg_len = live_meta.avg_len if reusable and live_meta else average_length(all_texts)

        await store.create_collection(name, settings.embed.dimension)
        meta = BuildMeta(
            collection=name,
            embedding_model=settings.embed.model,
            dimension=settings.embed.dimension,
            document_prefix=settings.embed.document_prefix,
            chunker_version=fingerprint,
            sparse_version=SPARSE_VERSION,
            avg_len=avg_len,
        )
        for doc_id, pd in parsed.items():
            previous = live_meta.docs.get(doc_id) if reusable and live_meta else None
            if previous and previous.sha256 == hashes[doc_id] and live:
                copied = await store.copy_document(live, name, doc_id)
                report.copied_docs += 1
                logger.info("document_copied", doc=doc_id, chunks=copied)
            else:
                await _embed_and_upsert(store, embedder, name, pd.chunks, avg_len)
                report.embedded_docs += 1
            meta.docs[doc_id] = DocBuildInfo(sha256=hashes[doc_id], chunks=len(pd.chunks), title=pd.title)
        for doc_id in carried:
            if live and live_meta and doc_id in live_meta.docs:
                copied = await store.copy_document(live, name, doc_id)
                meta.docs[doc_id] = live_meta.docs[doc_id]
                report.copied_docs += 1
                logger.info("document_carried_over", doc=doc_id, chunks=copied)
            else:
                logger.warning("document_not_in_live_index", doc=doc_id)
                report.problems.append(f"{doc_id}: not in the live index; run a full build to add it")

        # 4. checks ---------------------------------------------------------------
        carried_chunks = sum(meta.docs[d].chunks for d in carried if d in meta.docs)
        report.checks = await _check(store, embedder, name, parsed, carried_chunks)
        failed = [c for c in report.checks if not c["ok"]]
        if failed:
            report.status = f"failed: {len(failed)} checks failed (live index unchanged)"
            await store.delete_collection(name)
            return report

        # 5. go live ----------------------------------------------------------------
        await store.write_meta(meta)
        await store.set_live(name)
        pruned = await store.prune(keep=name)
        report.status = f"live: {name}" + (f" (removed old builds: {pruned})" if pruned else "")
    except BaseException:
        report.status = "failed: error during build (live index unchanged)"
        try:
            await store.delete_collection(name)
        except Exception as cleanup_error:  # pragma: no cover - best effort
            logger.warning("cleanup_failed", error=repr(cleanup_error))
        raise
    finally:
        report.seconds = round(time.perf_counter() - started, 1)
        await embedder.aclose()
        if owns_store:
            await store.close()
    return report


async def _embed_and_upsert(
    store: QdrantStore, embedder: EmbeddingClient, name: str, chunks: list[Chunk], avg_len: float
) -> None:
    for start in range(0, len(chunks), EMBED_SLICE):
        part = chunks[start : start + EMBED_SLICE]
        texts = [c.embed_text for c in part]
        dense = await embedder.embed_documents(texts)
        sparse = [doc_vector(t, avg_len) for t in texts]
        await store.upsert_chunks(name, part, dense, sparse)
        logger.info("embedded", doc=part[0].metadata.doc_id, done=start + len(part), total=len(chunks))


async def _check(
    store: QdrantStore,
    embedder: EmbeddingClient,
    name: str,
    parsed: dict[str, ParsedDocument],
    carried_chunks: int = 0,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    expected_total = sum(len(p.chunks) for p in parsed.values()) + carried_chunks
    total = await store.count(name)
    checks.append(
        {"check": "total chunks", "ok": total == expected_total, "detail": f"{total}/{expected_total}"}
    )
    for doc_id, pd in parsed.items():
        flt = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        n = await store.count(name, flt)
        if n != len(pd.chunks):
            checks.append({"check": f"{doc_id} chunks", "ok": False, "detail": f"{n}/{len(pd.chunks)}"})
        for phrase in pd.spec.expect:
            hits = await store.hybrid_search(
                name,
                [await embedder.embed_query(phrase)],
                query_vector(phrase),
                k_dense=20,
                k_sparse=20,
                limit=10,
                flt=SearchFilter(doc_ids=[doc_id]),
            )
            found = any(normalize_hit(h.chunk.text, phrase) for h in hits)
            checks.append(
                {
                    "check": f"{doc_id}: finds {phrase!r}",
                    "ok": found,
                    "detail": hits[0].chunk.metadata.label if hits else "no hits",
                }
            )
    return checks


def normalize_hit(text: str, phrase: str) -> bool:
    from lexai.ingestion.parsing import normalize_for_match

    return normalize_for_match(phrase) in normalize_for_match(text)


def write_report(report: BuildReport, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"ingest-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    return path
