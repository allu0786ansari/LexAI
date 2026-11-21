# LexAI security

How LexAI's controls map to the **OWASP Top 10 for LLM Applications 2026** (published
August 2026; it supersedes the 2025 list the plan referred to), plus the web-application
controls around them. Each row names the control and where it lives, and says what is
not covered yet. Report a vulnerability privately to the repository owner, not in a
public issue.

## Threat model in one paragraph

LexAI answers questions about Indian law from a fixed, public corpus. Users are anonymous;
there are no accounts. The model has **no tools**: it reads passages and writes text, and
cannot call APIs, run code or change data. What there is to protect: other users'
conversations (sessions), personal data people type into questions, the service's
availability and cost (GPU time), the integrity of the index (only manifest documents),
the admin reindex endpoint, and above all the correctness of what it tells people about
the law.

## OWASP LLM Top 10 (2026)

| Risk | Controls in LexAI | Not covered yet |
| --- | --- | --- |
| **LLM01 Prompt Injection** | The prompt marks passages as evidence, not instructions, and (`answer_v4`) tells the model to answer only the legal part of a message that tries to change its rules (`generation/templates/`). Requests to help with a crime are refused by rules before search (`query/understand.py`). The model has no tools, so a successful injection can only change text, and every answer is checked against its sources (`generation/verify.py`). Prompt-injection questions are in the golden set: 3/3 resisted in Phase 4. | The model still declines injected questions instead of answering their legal part, and in one case listed passage headings it was told to print (no instructions revealed). |
| **LLM02 Sensitive Information Disclosure** | Phone, e-mail, Aadhaar- and PAN-like numbers are masked in logs, in traces (question/answer text is masked unless `LEXAI_OBS_CAPTURE_TEXT=true` for a trace backend you control) and in stored feedback comments. Sessions are server-issued UUIDs; an unknown id is refused (404), so one client cannot read or write another's conversation (B11). Errors return a code and request id, never stack traces or upstream details. The corpus is public law. | Conversations are kept in memory or Redis for 30 minutes; there is no user-facing "delete my session" endpoint. |
| **LLM03 Excessive Agency** | No tools, plugins or function calling. The only state-changing operations (reindex) are admin endpoints behind an API key, not reachable by the model. | — |
| **LLM04 Supply Chain** | Python dependencies locked (`uv.lock`, generated `requirements*.txt`); `pip-audit` and `npm audit` run in CI. Source documents are pinned by SHA-256 (`data/manifest.lock.json`, `data/bundled/SHA256SUMS`). Models come from the Ollama registry by tag, configured in one place. | Image scanning (Trivy) and pinned base-image digests arrive with the container images (Phase 6). Model weights are pinned by tag, not digest. |
| **LLM05 Data and Model Poisoning** | Only documents in the reviewed manifest are indexed, from official sources (India Code, the Gazette via PRS), with checksum and expected-phrase checks before a build can go live; a failing check leaves the live index unchanged (`ingestion/indexer.py`). Reindexing is admin-only. Blue/green builds keep the previous index for rollback. | — (no fine-tuning; the model is used as published) |
| **LLM06 Unbounded Consumption** | Rate limits per client IP and per session (default 20 chats/min, 429 + `Retry-After`; Redis-backed for several workers). Request bodies ≤ 16 KB; questions ≤ 2,000 characters; answers ≤ `LEXAI_LLM_MAX_TOKENS`; passages ≤ the context budget. A client that disconnects stops generation upstream. Timeouts on every model call. One reindex job at a time. | No admission control for concurrent generation: with one GPU, extra answers queue in Ollama (Phase 4 load test). A bounded queue that rejects with a clear message belongs to the deployment phase. |
| **LLM07 Misinformation** | Answers are grounded in retrieved passages with `[n]` citations; the verifier removes invalid citations and flags provisions not in the sources; the UI shows the result. Abstention when nothing relevant is found. The applicable code (old or new criminal codes) is decided by rules and stated. Quality is measured on a 200-question golden set and guarded by a CI gate (Phase 4). Every answer ends with "legal information, not legal advice". | Known weaknesses in `runlogs/phase4/RESULTS.md` (e.g. example leakage in 3/40 answers, all flagged by the verifier). The golden set was not reviewed by a lawyer. |
| **LLM08 Hidden Context Exposure** | The system prompt contains no secrets, keys or internal data: only answering rules and the retrieved passages, which the user sees as citations anyway. Secrets live in environment variables, never in prompts. | — |
| **LLM09 Vector and Embedding Weaknesses** | The index contains only manifest documents; users cannot add documents. Single tenant, no per-user data in the index. At start-up the API refuses an index built with a different embedding model, dimension, prefix or BM25 version (`api/container.py`). Filters are applied by Qdrant on payload fields, not by the model. | — |
| **LLM10 Improper Output Handling** | The frontend renders answers as Markdown with raw HTML disabled and links limited to http(s). Model output is never executed, used in a query or passed to a shell. SSE payloads are JSON-encoded. API responses carry `Content-Security-Policy: default-src 'none'`. | A CSP for the frontend's own pages is set by nginx in Phase 6. |

## Web application controls

| Control | Where |
| --- | --- |
| Only server-issued session ids; 404 for unknown/expired ids (B11) | `services/sessions.py`, `api/routes.py` |
| Rate limits per IP and per session, 429 with `Retry-After` | `services/ratelimit.py` |
| Body size limit (16 KB, with or without `Content-Length`) | `api/middleware.py` |
| CORS: explicit origin list, no credentials | `api/app.py`, `LEXAI_API_CORS_ORIGINS` |
| Security headers: `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, CSP | `api/middleware.py` |
| Admin endpoints: API key from the environment, constant-time comparison; disabled when unset | `api/routes.py`, `LEXAI_API_ADMIN_API_KEY` |
| OpenAPI docs disabled when `LEXAI_API_ENVIRONMENT=production` | `api/app.py` |
| PDFs served only by manifest document id, never by a client path | `api/container.py` (`pdf_path`) |
| Errors: `{code, message, request_id}`, details only in logs | `api/app.py` |
| Secrets only in environment variables; `.env` is git-ignored | `deploy/.env.example` |
| Behind a proxy: run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so rate limits see the real client IP | Phase 6 compose |

## Checks

- `tests/api/test_v1.py`: forged sessions, 429 + `Retry-After`, 413, security headers, admin key, path traversal on PDFs, error shape.
- `tests/unit/test_state_and_telemetry.py`: session isolation and expiry (memory and Redis), PII masking on traces.
- CI: `pip-audit`, `npm audit --audit-level=high`.
