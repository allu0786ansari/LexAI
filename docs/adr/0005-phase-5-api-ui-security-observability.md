# ADR 0005 — Phase 5 API v1, TypeScript UI, security and observability

Date: 2026-11-21 · Status: proposed (pending the laptop run, runlogs/phase5)

## Context

After Phase 4 the pipeline was measured and guarded, but not a product someone else could
run and debug: the API accepted any session id a client made up (B11), had no way to open
a cited passage, counted nothing and traced nothing; the frontend rewrote every chat to
`localStorage` on every token (B12) and had no stop button or mobile navigation (B13).

## Decisions

1. **One versioned API, v0.1 routes removed.** `/api/v1/...`: sessions, chat (SSE),
   search, chunks, documents (+ PDF), feedback, admin reindex; plus `/healthz`,
   `/readyz`, `/metrics`. The old `/api/query|search|session` are gone, not aliased: the
   only client is this repository's frontend, rewritten in the same change. Every error
   is `{code, message, request_id}` with a meaningful status.
2. **Server-issued sessions only (B11).** `POST /sessions` creates a UUIDv4; `/chat` and
   `/search` refuse an unknown or expired id with 404 (omitting the id starts a new
   session). The client creates a new session once and retries when its id has expired.
3. **State behind interfaces: in-process by default, Redis when configured.** Sessions
   (last 20 messages, 30-minute sliding TTL), rate limits and the answer cache share one
   `LEXAI_API_REDIS_URL`. The laptop has no Docker and too little RAM for extra services,
   so it runs in-process; Phase 6's Compose stack sets the URL for several workers. Both
   paths are tested (Redis against a real server in CI).
4. **Own rate limiter instead of slowapi.** Limits apply per client IP *and* per session,
   and the session id is in the JSON body, which slowapi's key functions cannot read. A
   small wrapper over the `limits` library (moving window, memory or Redis via redis-py)
   is called by the routes after validation; 429 carries `Retry-After`.
5. **Security controls at the edge of the app.** Body ≤ 16 KB (checked with or without
   `Content-Length`), security headers and a `default-src 'none'` CSP on API responses,
   admin key compared in constant time (admin disabled when unset), docs off in
   production, PDFs served only by manifest id. Mapped to the OWASP Top 10 for LLM
   Applications **2026** edition (current since August 2026) in `docs/security.md`.
6. **Tracing with OpenTelemetry, exported where the operator chooses.** FastAPI 0.165 opens
   the HTTP server span itself; LexAI adds a `lexai.chat` span and one child per stage
   (condense, retrieve with chunk ids and scores, context, generate with GenAI attributes,
   verify). Spans that cover streamed output are ended explicitly rather than made
   "current", because context must not be held across `yield`. Export: OTLP/HTTP
   (Langfuse accepts it) and/or a JSON-lines file for machines without a collector, read
   with `scripts/show_trace.py`. Question and answer text is PII-masked on traces unless
   `LEXAI_OBS_CAPTURE_TEXT=true`. The trace id is sent in the `meta` event.
7. **Prometheus metrics with bounded labels.** Route templates (never raw paths), fixed
   outcome/stage/service sets. The Grafana dashboard and alert rules are committed as code;
   a test checks they only query exported metrics, and CI validates the rules with promtool.
   Readiness is probed in the background so `lexai_ready` can drive the 2-minute alert.
8. **Reindex in-process, against the live store.** Embedded Qdrant allows one client per
   process, so `build_index` takes the API's store; on success the API swaps in a new chat
   service for the new collection (the previous build is kept, so requests in flight
   finish). One job at a time. Partial builds (`only`) now carry every other document
   over from the live index — before this phase `--only` silently published an index
   with only the selected documents.
9. **Frontend: TypeScript with types generated from the API.** `export_openapi.py` writes
   the schema; `openapi-typescript` generates `schema.d.ts`; CI fails if either drifts. The
   SSE payloads are described by a `ChatEvents` model referenced from `/chat`'s response,
   so the stream is typed too. State is a reducer; streaming text stays in memory and is
   saved once per finished answer (B12). Stop (AbortController), regenerate, copy,
   feedback, filters, a citation side panel with the PDF page, a verification badge, a
   mobile drawer with New chat (B13), an `aria-live` answer region. Markdown is rendered
   with raw HTML skipped and links limited to http(s). Fonts are bundled.
10. **Tests at three levels.** Python: API contract, sessions, limits, security, tracing
    (in-memory exporter), Redis paths. Vitest: SSE parser, reducer (including "no save
    per token"), citation links. Playwright: the built UI against a mocked API, desktop
    and phone.

## Consequences

- Exit criteria checked on the laptop by `scripts/check_phase5.py`; the live Grafana and
  Langfuse criteria need those services, which arrive with Phase 6's Compose stack (the
  dashboard, alerts and OTLP export are ready for them).
- One API process on the laptop; more workers need Redis (Phase 6).
- No admission control for concurrent generation yet (Phase 4 load test); Phase 6.
