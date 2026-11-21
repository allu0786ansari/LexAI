# LexAI — legal research assistant for Indian law

LexAI answers questions about Indian law from a local corpus of statutes,
Supreme Court judgments and government manuals. It retrieves the relevant
passages, generates an answer with **Qwen2.5-7B-Instruct**, and cites every
claim as `[n]` so you can check the source.

> **Status: Phase 4 of 6 complete** (evaluation harness and CI regression gate;
> [verified results](runlogs/phase4/RESULTS.md)). The full plan covers ingestion,
> retrieval, evaluation, API/UI, observability and deployment.

**Measured on an 8 GB laptop** (200-question golden set, held-out test split, Qwen2.5-7B):

| | |
| --- | --- |
| Right provision ranked first / in top 5 (test split) | 94.6% / 96.0% (MRR@10 0.953) |
| Answers passing citation verification | 87.5% (gold provision cited in 87.9%) |
| Unanswerable questions declined · crime-assistance requests refused | 100% · 100% |
| Typical answer on the laptop | first token ~21 s, complete ~51 s |

The golden set was written for this corpus and checked against it automatically; it was
not reviewed by a lawyer. Details, ablations and known weaknesses: [Phase 4 results](runlogs/phase4/RESULTS.md).

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
cd frontend && npm install && npm run dev   # http://localhost:5173 (proxies /api to :8000)
```

React + TypeScript; types generated from the API's OpenAPI schema. Every `[n]` in an
answer opens the cited passage with a link to its PDF page. See
[`frontend/README.md`](frontend/README.md). API docs: http://localhost:8000/docs

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/sessions` | Create a conversation (server-issued id; 30-minute idle expiry) |
| `POST` | `/api/v1/chat` | Ask a question. Server-Sent Events: `meta` → `citations` → `token`* → `verification` → `done`, or `error`. Optional `filters`: acts, in force only |
| `POST` | `/api/v1/search` | Retrieval only: the plan, passages, notes and scores the model would get |
| `GET` | `/api/v1/chunks/{chunk_id}` | A cited passage: the whole provision, its metadata and a link to the PDF page |
| `GET` | `/api/v1/documents` | The corpus: documents, types, legal status, indexed passages |
| `GET` | `/api/v1/documents/{doc_id}/pdf` | The source PDF |
| `POST` | `/api/v1/feedback` | Thumbs up/down (+ comment) on an answer, by request id |
| `POST` | `/api/v1/admin/reindex` | Start an ingestion build (`X-API-Key`); the API switches to it when it goes live |
| `GET` | `/healthz`, `/readyz`, `/metrics` | Liveness, readiness (503 if not ready), Prometheus metrics |

Errors are JSON `{code, message, request_id}`: 404 unknown session/passage, 413 body over
16 KB, 422 invalid request, 429 rate limited (with `Retry-After`), 503 model server or index
unavailable. Every response carries `X-Request-ID`; the same id is in the logs, in the
`meta`/`done`/`error` events and on the request's trace. Security controls and their
mapping to the OWASP Top 10 for LLM Applications: [`docs/security.md`](docs/security.md).

### Observability

- **Traces** (OpenTelemetry): one trace per request with a span per stage — condense,
  retrieve (chunk ids and scores), context, generate (tokens, time to first token),
  verify. Export to Langfuse or any OTLP collector (`LEXAI_OBS_OTLP_*`), or to a local
  file (`LEXAI_OBS_TRACE_FILE`, on in the laptop profile) read with
  `python scripts/show_trace.py`.
- **Metrics** at `/metrics`: requests and latency per route, time to first token,
  latency per stage, answers by outcome, verification failures, cache hits, upstream
  errors, rate-limited requests. Dashboard: [`deploy/grafana/lexai.json`](deploy/grafana/lexai.json);
  alerts: [`deploy/prometheus/alerts.yml`](deploy/prometheus/alerts.yml).
- **State**: sessions, rate limits and the answer cache are in-process by default and
  move to Redis with `LEXAI_API_REDIS_URL` (needed for more than one API worker).

## Configuration

All settings are environment variables (or `backend/.env`), grouped by prefix:
`LEXAI_LLM_*`, `LEXAI_EMBED_*`, `LEXAI_RERANK_*`, `LEXAI_RETRIEVAL_*`, `LEXAI_CORPUS_*`,
`LEXAI_QDRANT_*`, `LEXAI_QUERY_*`, `LEXAI_CACHE_*`, `LEXAI_API_*`, `LEXAI_OBS_*`.
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

# Evaluation (results in eval/reports/<commit>/)
python -m lexai.evaluation validate            # check the golden set against the index
python -m lexai.evaluation retrieval --configs hybrid,dense,bm25,no-routing
python -m lexai.evaluation answers --limit 40  # full answers, deterministic metrics (+ judge if configured)
python -m lexai.evaluation report              # report.md from the JSON results
python -m lexai.evaluation offline             # CI retrieval gate on eval/fixtures (no model server)
python scripts/load_test.py                    # latency under concurrency
python scripts/check_phase5.py                 # API v1, security, sources, tracing, reindex (real stack)
python scripts/show_trace.py --list            # recent requests in the local trace file
python scripts/export_openapi.py               # refresh the schema the frontend types come from
```

The golden set, metrics, split and gate are described in
[ADR 0004](docs/adr/0004-phase-4-evaluation.md).

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
    evaluation/   golden-set loader, metrics, judge, offline fixture, `python -m lexai.evaluation`
  tests/          unit and API tests (no model servers needed)
  scripts/        model check, index inspection, smoke test, Phase 3 checks, load test
data/
  manifest.yaml   every source document and its metadata
  manifest.lock.json  checksums of downloaded files (written by `fetch`)
  bundled/        PDFs shipped with the repo + SHA256SUMS
  raw/            downloads (not in git)
eval/
  datasets/       golden set (200 items) and its builder; test-split lock
  fixtures/       exported index + smoke queries for the offline CI gate
  baselines/      gate baseline (Recall@8, MRR@10)
  reports/        results per commit (JSON + report.md)
deploy/           Ollama Modelfiles, env profiles, vLLM notes, Grafana dashboard, Prometheus alerts
.github/workflows/ CI: lint, types, tests (Linux + Windows/pip), retrieval gate, frontend build
docs/adr/         architecture decision records
docs/security.md  security controls mapped to the OWASP Top 10 for LLM Applications
runlogs/          per-phase verification scripts and their output
frontend/         React + TypeScript chat UI (types from the OpenAPI schema; Vitest + Playwright)
```

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Foundation: async clients, Qwen2.5-7B, fail-loudly, tests | **done** ([results](runlogs/phase1/RESULTS.md)) |
| 2 | Ingestion: corpus manifest, BNS/BNSS/BSA, legal-structure chunking, Qdrant | **done** ([results](runlogs/phase2/RESULTS.md)) |
| 3 | Retrieval & generation: query routing, citation verification, abstention | **done** ([results](runlogs/phase3/RESULTS.md)) |
| 4 | Evaluation: golden set, retrieval/answer metrics, ablations, load test, CI gate | **done** ([results](runlogs/phase4/RESULTS.md)) |
| 5 | API v1, TypeScript UI, security, observability | in review |
| 6 | Docker Compose, CI/CD, deployment | planned |

## Disclaimer

LexAI provides legal information for research, not legal advice. Answers can
be incomplete or wrong; always verify against the primary source. Note that
from 1 July 2024 the BNS, BNSS and BSA replaced the IPC, CrPC and Indian
Evidence Act. The corpus contains both; the older codes still govern offences
committed before that date, and LexAI marks them as repealed.
