# LexAI — legal research assistant for Indian law

LexAI answers questions about Indian law from a local corpus of statutes,
Supreme Court judgments and government manuals. It retrieves the relevant
passages, generates an answer with **Qwen2.5-7B-Instruct**, and cites every
claim as `[n]` so you can check the source.

> **Status: Phase 3 of 6 in verification** (query understanding and grounded
> answers; Phase 2 [verified](runlogs/phase2/RESULTS.md)). The full plan covers ingestion,
> retrieval, evaluation, API/UI, observability and deployment. Quality numbers
> will be published here only once Phase 4's evaluation produces them.

## How it works

```text
data/manifest.yaml ─▶ fetch (download + SHA-256 pin) ─▶ legal parser ─▶ embed ─▶ Qdrant (new collection,
                                                       (sections, articles,      checks, then alias swap)
                                                        judgments)

Browser (React) ──SSE──▶ FastAPI (async)
                           │
                           ├─▶ Understand  follow-up rewrite (Qwen, 2nd turn on) · references · intent ·
                           │               which code applies · guardrails            (lexai/query)
                           ├─▶ Retrieve    exact sections pinned + old↔new counterparts ·
                           │               hybrid search in Qdrant (dense + BM25, RRF) ·
                           │               reranker (server profile) · abstain on low score
                           ├─▶ Generate    qwen2.5-7b-instruct (Ollama or vLLM), versioned prompt
                           └─▶ Verify      citation markers, cited provisions, coverage → `verification`
```

- **Named sections are exact.** "What does Section 103 BNS say?" fetches BNS
  s.103 by filter and puts it first, with its IPC counterpart.
- **Old and new codes linked.** Every IPC/CrPC/IEA section is matched to its
  BNS/BNSS/BSA counterpart by title and text (a derived table, shown as
  "corresponds to"). Answers follow the code that applies: the new codes unless
  the events predate 1 July 2024 or the question asks about the old code.
- **Checked answers.** After streaming, invalid citation markers are removed and
  any section the answer names that is not in its sources is flagged; the UI
  shows the result.

- **Pinned, current corpus.** [`data/manifest.yaml`](data/manifest.yaml) lists
  every source: the BNS, BNSS and BSA (in force since 1 July 2024) and the IPC
  and Evidence Act are downloaded and pinned by checksum in
  `data/manifest.lock.json`; the Constitution, CrPC, IT Act, 50 Supreme Court
  judgments and 3 MHA manuals are bundled in `data/bundled/`.
- **Legal-structure chunks.** Statutes are split at section and article
  boundaries with their chapter/part, title and page; amendment footnotes are
  removed; judgments carry case name, date and bench. Repealed codes are marked
  as such, and that status appears in the prompt and in each citation
  ("IPC s.302 — repealed 2024-07-01; replaced by BNS").
- **Safe rebuilds.** Each build writes a new Qdrant collection, checks counts
  and that known provisions are findable, then moves the `lexai_chunks` alias.
  Unchanged documents are copied rather than re-embedded.

- **Async end to end.** Model calls use an async HTTP client and true token
  streaming; CPU-bound search and reranking run in worker threads, so one
  answer never blocks other requests.
- **Fails loudly.** No fallback embeddings. If a model server is down, `/readyz`
  turns red and the chat returns an `error` event with a request id.
- **Index safety.** The index records the embedding model, dimension, prefix
  and sparse scheme that built it; the API refuses to start against a mismatch.
- **One model interface.** Ollama (dev) and vLLM (prod) both speak the
  OpenAI-compatible API, under the same model name.

## Quick start

Prerequisites: Python 3.12, [Ollama](https://ollama.com), Node 18+ for the UI.

### Windows (cmd) — 8 GB laptop profile

```bat
runlogs\phase2\run_phase2.bat setup models fetch build
cd backend
.venv\Scripts\python.exe -m uvicorn lexai.api.app:create_app --factory --port 8000
```

`setup` creates `backend\.venv` with pip and writes `backend\.env` from
[`deploy/env/laptop-8gb.env`](deploy/env/laptop-8gb.env): Qwen2.5-7B with a 4K
context, `nomic-embed-text` embeddings, reranker off, smaller chunks. See
[`runlogs/phase2/README.md`](runlogs/phase2/README.md) for details. The index
is embedded Qdrant in `backend\var\qdrant`: stop the API before rebuilding.

### Linux / macOS / GPU server

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M && ollama pull qwen3-embedding:0.6b
ollama create qwen2.5-7b-instruct -f deploy/ollama/Modelfile     # 8K context
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -r requirements-rerank.txt && pip install --no-deps -e .
python -m lexai.ingestion.cli fetch && python -m lexai.ingestion.cli build
uvicorn lexai.api.app:create_app --factory --port 8000
```

(`uv sync --extra rerank` works too; `requirements*.txt` are exported from `uv.lock`.)
To use a Qdrant server instead of the embedded store, set `LEXAI_QDRANT_URL`
(e.g. `http://localhost:6333`); Phase 6 adds it to Docker Compose.

### Frontend

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

API docs: http://localhost:8000/docs

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/query` | Ask a question. Server-Sent Events (contract v1): `meta` → `citations` → `token`* → `verification` → `done`, or `error` |
| `POST` | `/api/search` | Retrieval only: the plan, passages, notes and scores the model would get |
| `POST` | `/api/session` | Create a chat session id |
| `GET` | `/healthz` | Liveness |
| `GET` | `/readyz` | Readiness: LLM served, embedder dimension, index loaded (503 if not) |

Every response carries an `X-Request-ID` header; the same id is in the logs and in `done`/`error` events.

## Configuration

All settings are environment variables (or `backend/.env`), grouped by prefix:
`LEXAI_LLM_*`, `LEXAI_EMBED_*`, `LEXAI_RERANK_*`, `LEXAI_RETRIEVAL_*`, `LEXAI_CORPUS_*`,
`LEXAI_QDRANT_*`, `LEXAI_QUERY_*`, `LEXAI_CACHE_*`, `LEXAI_API_*`.
See [`deploy/.env.example`](deploy/.env.example) for every option and its default.
To serve the model with vLLM on a GPU, see [`deploy/vllm/README.md`](deploy/vllm/README.md).

## Development

From `backend/`, with the virtual environment's Python (`.venv\Scripts\python.exe` on Windows):

```bash
python -m ruff check . && python -m ruff format --check .
python -m mypy
python -m pytest
python -m lexai.ingestion.cli fetch            # download and verify sources
python -m lexai.ingestion.cli build --dry-run  # parse and chunk only, no model calls
python -m lexai.ingestion.cli build --only "sc-*"   # rebuild a subset (others copied)
python -m lexai.ingestion.cli status           # the live index and its documents
python scripts/inspect_index.py                # look up provisions, run test searches
python scripts/check_models.py                 # are the configured models served and working?
```

With uv/make: `make check`. Git hooks: `pre-commit install`.

## Project layout

```text
backend/
  src/lexai/
    api/          FastAPI app factory, routes, request-id middleware, dependency container
    clients/      async LLM, embedding and reranker clients
    core/         settings, structured logging, typed errors
    domain/       Pydantic models (Chunk, Citation, SSE events)
    generation/   versioned prompt templates, context assembly, citation verifier, chat service
    ingestion/    manifest, fetch, PDF parsing, legal chunkers, index builder, `lexai-ingest` CLI
    query/        reference extraction, intent, applicable code, guardrails, follow-up rewriting
    retrieval/    Qdrant store (dense + BM25 sparse, RRF), hybrid retriever, router, old↔new correspondence
    services/     session memory, answer cache
  tests/          unit and API tests (no model servers needed)
  scripts/        model check, index inspection, smoke test, Phase 3 checks against a running stack
data/
  manifest.yaml   every source document and its metadata
  manifest.lock.json  checksums of downloaded files (written by `fetch`)
  bundled/        PDFs shipped with the repo + SHA256SUMS
  raw/            downloads (not in git)
deploy/           Ollama Modelfiles (server, laptop), env profiles, vLLM notes
docs/adr/         architecture decision records
runlogs/          per-phase verification scripts and their output
frontend/         React + Vite chat UI
```

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Foundation: async clients, Qwen2.5-7B, fail-loudly, tests | **done** ([results](runlogs/phase1/RESULTS.md)) |
| 2 | Ingestion: corpus manifest, BNS/BNSS/BSA, legal-structure chunking, Qdrant | **done** ([results](runlogs/phase2/RESULTS.md)) |
| 3 | Retrieval & generation: query routing, citation verification, abstention | **in verification** ([run](runlogs/phase3/README.md)) |
| 4 | Evaluation: golden set, retrieval metrics, RAGAS, CI gate | planned |
| 5 | API v1, TypeScript UI, security, observability | planned |
| 6 | Docker Compose, CI/CD, deployment | planned |

## Disclaimer

LexAI provides legal information for research, not legal advice. Answers can
be incomplete or wrong; always verify against the primary source. Note that
from 1 July 2024 the BNS, BNSS and BSA replaced the IPC, CrPC and Indian
Evidence Act. The corpus contains both; the older codes still govern offences
committed before that date, and LexAI marks them as repealed.
