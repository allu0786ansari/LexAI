# ADR 0002 — Phase 2 ingestion and index decisions

Date: 2026-11-20 · Status: proposed (pending verification on the target laptop, see runlogs/phase2/)

## Context

The v0.1 corpus was a folder of PDFs in `backend/Data`, split into fixed-size
character windows that ignored section boundaries. It held only the IPC, CrPC
and Evidence Act, which the BNS, BNSS and BSA replaced on 1 July 2024, and its
IPC PDF stopped partway through the Code. Retrieval used FAISS plus an in-memory
BM25 index rebuilt at every start. Phase 2 makes the corpus current and
reproducible, chunks it the way lawyers cite it, and moves the index to a
database that can run embedded on a laptop and as a server in production.

## Decisions

1. **A manifest describes the corpus.** `data/manifest.yaml` lists each document
   with its id, short name, type, year, commencement, repeal date and
   replacement, and either download URLs (tried in order) or a bundled file.
   Judgments are a glob collection. Each statute declares `expect` phrases and
   `min_units` so a bad PDF fails the build instead of producing a thin index.
2. **Downloads are pinned.** `fetch` writes each file's SHA-256 to
   `data/manifest.lock.json`, which is committed. A later download with a
   different checksum is refused unless `--update` is passed, so a changed
   upstream file is a reviewed change, not a silent one. Bundled files are
   checked against `data/bundled/SHA256SUMS`. Responses must be real PDFs, and
   writes are atomic. Government sites are often overloaded (India Code answered
   504 for all three new codes on the first laptop run), so 429/5xx, timeouts and
   dropped connections are retried with backoff, each document lists fallback
   sources, and the source that produced the pin is tried first.
3. **Legal-structure chunking.** Statutes are split at section/article headings
   (`302. Punishment for murder.—`), with section numbers required to increase
   so the arrangement table and cross-references are not mistaken for headings.
   Each unit keeps its chapter/part, title and pages. Long units are split at
   paragraph boundaries; the pieces share a `parent_id` and repeat the header.
   Footnotes recording amendments are removed. Judgments keep case name, date,
   bench and citation from the JUDIS header. The chunker version and settings
   form a fingerprint stored with the index; changing them re-chunks on the
   next build.
4. **Gazette of India prints are parsed by position.** The Gazette print of an
   Act has no arrangement table and no heading dash; each section's title is a
   marginal note level with its first line, and text extraction emits those
   notes out of order. `gazette.py` records where every text fragment is drawn,
   matches each note to the section number at the same height (within 6
   points), and removes notes, mastheads, running heads and legacy-font Hindi
   from the body, along with the printer's colophon. Column positions (section
   numbers, body edge) are measured over the whole document, and only fragments
   drawn inside the page are used: the PRS prints draw an invisible copy of the
   running head below the page, which threw off a per-page estimate on every
   even page. This makes the PRS India Gazette copies usable fallbacks for BNS,
   BNSS and BSA (all 358 / 531 / 170 sections titled) and turns the CLA 2018
   print into 26 titled sections.
   Statutes in neither layout (the Maharashtra Prisons Act compilation) are
   chunked as plain text with a warning when `min_units` is 0.
5. **Repeal status is data, not prose.** `repealed_on` and `replaced_by` travel
   with each chunk into the prompt header and the citation, so an IPC passage is
   presented as "repealed 2024-07-01; replaced by BNS; applies to earlier
   offences". Judgments and manuals carry no status.
6. **Qdrant for the index.** Dense vectors and BM25 sparse vectors live in one
   collection and are fused with Reciprocal Rank Fusion inside Qdrant (prefetch
   + RRF), replacing FAISS + rank-bm25. Payload filters (document, type, in
   force only) come for free for Phase 3. The same code runs against embedded
   Qdrant (`LEXAI_QDRANT_PATH`, no Docker, for the 8 GB laptop) or a server
   (`LEXAI_QDRANT_URL`, for Docker Compose in Phase 6).
7. **BM25 term weights are computed client-side.** Document vectors hold the
   saturated term frequency (k1 = 1.2, b = 0.75) over CRC32 term ids; Qdrant's
   IDF modifier supplies the IDF at query time. This avoids a tokenizer model
   download (fastembed) and is stable across processes. The scheme is versioned
   (`bm25-crc32-v1`) and stored with the index.
8. **Blue/green builds behind an alias.** Each build writes a new collection,
   then checks the total and per-document chunk counts and that every `expect`
   phrase is retrieved for its document. Only then does the `lexai_chunks`
   alias move; one previous collection is kept for rollback. A failed build
   deletes its collection and leaves the live index untouched. Unchanged
   documents (same checksum, same chunker and embedder) are copied from the
   live collection instead of re-embedded.
9. **The index records what built it.** A `lexai_meta` collection stores the
   embedding model, dimension, document prefix, chunker fingerprint, sparse
   scheme and per-document checksums. The API refuses to serve an index that
   does not match its configuration.
10. **Synchronous Qdrant client in worker threads.** The embedded mode of the
    async client still does its work on the event loop; in testing it stalled
    `/healthz` by ~100 ms per search. All store calls use the sync client via
    `asyncio.to_thread`, serialised by a lock in embedded mode (worst stall
    measured afterwards: 9 ms).
11. **pypdf stays the PDF extractor.** The sources are born-digital; pypdf
    extracts them in reading order and is pure Python (no system packages on
    Windows). OCR and layout models are unnecessary for this corpus.

## Consequences

- The laptop profile uses 1,400-character chunks and a 6,000-character passage
  budget so four to five passages fit in Qwen's 4K context; the server profile
  keeps 2,400 and 12,000.
- Embedded Qdrant allows one process at a time: the API must be stopped while
  building. Production uses the server mode, where this does not apply.
- The chunker version moved to `legal-v3` with the Gazette parser, so the first
  build after it re-parses and re-embeds everything.
- Search quality is only smoke-tested in this phase (`scripts/inspect_index.py`).
  Retrieval metrics and the reranker comparison come with the evaluation set in
  Phases 3–4.
