# LexAI — legal research assistant for Indian law

LexAI answers questions about Indian law from a local corpus of statutes,
Supreme Court judgments and government manuals. It retrieves the relevant
passages, generates an answer with **Qwen2.5-7B-Instruct**, and cites every
claim as `[n]` so you can check the source.

> **Status: Phase 1 of 6 complete** (foundation; [verified results](runlogs/phase1/RESULTS.md)). The full plan covers ingestion,
> retrieval, evaluation, API/UI, observability and deployment. Quality numbers
> will be published here only once Phase 4's evaluation produces them.

## How it works (Phase 1)

```text
Browser (React) ──SSE──▶ FastAPI (async)
                           │
                           ├─▶ Embedder  qwen3-embedding:0.6b   (Ollama /v1/embeddings)
                           ├─▶ Hybrid search: FAISS (dense) + BM25 (sparse), fused with RRF
                           ├─▶ Reranker  BAAI/bge-reranker-v2-m3 (in-process, worker thread)
                           └─▶ Generator qwen2.5-7b-instruct    (Ollama or vLLM, /v1/chat/completions)
```

- **Async end to end.** Model calls use an async HTTP client and true token
  streaming; CPU-bound search and reranking run in worker threads, so one
  answer never blocks other requests.
- **Fails loudly.** No fallback embeddings. If a model server is down, `/readyz`
  turns red and the chat returns an `error` event with a request id.
- **Index safety.** The index records the embedding model and dimension that
  built it; the API refuses to start against a mismatch. Ingestion builds a new
  index beside the live one and swaps it in only after it loads cleanly.
- **One model interface.** Ollama (dev) and vLLM (prod) both speak the
  OpenAI-compatible API, under the same model name.

## Quick start

Prerequisites: Python 3.12, [Ollama](https://ollama.com), Node 18+ for the UI.

### Windows (cmd) — 8 GB laptop profile

```bat
runlogs\phase1\run_phase1.bat setup models ingest
cd backend
.venv\Scripts\python.exe -m uvicorn lexai.api.app:create_app --factory --port 8000
```

`setup` creates `backend\.venv` with pip and writes `backend\.env` from
[`deploy/env/laptop-8gb.env`](deploy/env/laptop-8gb.env): Qwen2.5-7B with a 4K
context, `nomic-embed-text` embeddings, reranker off. See
[`runlogs/phase1/README.md`](runlogs/phase1/README.md) for details.

### Linux / macOS / GPU server

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M && ollama pull qwen3-embedding:0.6b
ollama create qwen2.5-7b-instruct -f deploy/ollama/Modelfile     # 8K context
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -r requirements-rerank.txt && pip install --no-deps -e .
python -m lexai.ingestion.cli
uvicorn lexai.api.app:create_app --factory --port 8000
```

(`uv sync --extra rerank` works too; `requirements*.txt` are exported from `uv.lock`.)

### Frontend

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

API docs: http://localhost:8000/docs

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/query` | Ask a question. Server-Sent Events: `citations` → `token`* → `done`, or `error` |
| `POST` | `/api/session` | Create a chat session id |
| `GET` | `/healthz` | Liveness |
| `GET` | `/readyz` | Readiness: LLM served, embedder dimension, index loaded (503 if not) |

Every response carries an `X-Request-ID` header; the same id is in the logs and in `done`/`error` events.

## Configuration

All settings are environment variables (or `backend/.env`), grouped by prefix:
`LEXAI_LLM_*`, `LEXAI_EMBED_*`, `LEXAI_RERANK_*`, `LEXAI_RETRIEVAL_*`, `LEXAI_API_*`.
See [`deploy/.env.example`](deploy/.env.example) for every option and its default.
To serve the model with vLLM on a GPU, see [`deploy/vllm/README.md`](deploy/vllm/README.md).

## Development

From `backend/`, with the virtual environment's Python (`.venv\Scripts\python.exe` on Windows):

```bash
python -m ruff check . && python -m ruff format --check .
python -m mypy
python -m pytest
python -m lexai.ingestion.cli --dry-run   # parse and chunk only, no model calls
python scripts/check_models.py            # are the configured models served and working?
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
    generation/   prompts and the chat orchestration service
    ingestion/    PDF loading, splitting, metadata, `lexai-ingest` CLI
    retrieval/    FAISS + BM25 index, RRF fusion, hybrid retriever
    services/     session memory
  tests/          unit and API tests (no model servers needed)
  scripts/        model check and smoke test against a running stack
  Data/           source PDFs (moves to a manifest + download script in Phase 2)
deploy/           Ollama Modelfiles (server, laptop), env profiles, vLLM notes
docs/adr/         architecture decision records
runlogs/          per-phase verification scripts and their output
frontend/         React + Vite chat UI
```

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Foundation: async clients, Qwen2.5-7B, fail-loudly, tests | **done** ([results](runlogs/phase1/RESULTS.md)) |
| 2 | Ingestion: corpus manifest, BNS/BNSS/BSA, legal-structure chunking, Qdrant | planned |
| 3 | Retrieval & generation: query routing, citation verification, abstention | planned |
| 4 | Evaluation: golden set, retrieval metrics, RAGAS, CI gate | planned |
| 5 | API v1, TypeScript UI, security, observability | planned |
| 6 | Docker Compose, CI/CD, deployment | planned |

## Disclaimer

LexAI provides legal information for research, not legal advice. Answers can
be incomplete or wrong; always verify against the primary source. Note that
from 1 July 2024 the BNS, BNSS and BSA replaced the IPC, CrPC and Indian
Evidence Act; the Phase 1 corpus still contains only the older codes.
