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
| **LLM04 Supply Chain** | Python dependencies locked (`uv.lock`, generated `requirements*.txt`); `pip-audit` and `npm audit` run in CI. Container images: every base and service image pinned to a version (no `latest`, checked by `tests/unit/test_compose_config.py`); both LexAI images scanned by Trivy in CI, failing on fixable critical vulnerabilities; releases carry an SBOM and a build-provenance attestation (`release.yml`); the Trivy action is pinned by commit after its 2026 tag hijack. Source documents are pinned by SHA-256 (`data/manifest.lock.json`, `data/bundled/SHA256SUMS`). Models come from the Ollama registry by tag, configured in one place. | Images and models are pinned by version tag, not digest (Dependabot-style digest updates are not set up). Model weights are pinned by tag. |
| **LLM05 Data and Model Poisoning** | Only documents in the reviewed manifest are indexed, from official sources (India Code, the Gazette via PRS), with checksum and expected-phrase checks before a build can go live; a failing check leaves the live index unchanged (`ingestion/indexer.py`). Reindexing is admin-only. Blue/green builds keep the previous index for rollback. | — (no fine-tuning; the model is used as published) |
| **LLM06 Unbounded Consumption** | Rate limits per client IP and per session (default 20 chats/min, 429 + `Retry-After`; Redis-backed for several workers). Request bodies ≤ 16 KB; questions ≤ 2,000 characters; answers ≤ `LEXAI_LLM_MAX_TOKENS`; passages ≤ the context budget. A client that disconnects stops generation upstream. Timeouts on every model call. One reindex job at a time. | No admission control for concurrent generation: with one GPU, extra answers queue in Ollama (Phase 4 load test). Phase 6 can run several API replicas behind nginx (shared limits in Redis), but a bounded generation queue that rejects with a clear message is still missing. |
| **LLM07 Misinformation** | Answers are grounded in retrieved passages with `[n]` citations; the verifier removes invalid citations and flags provisions not in the sources; the UI shows the result. Abstention when nothing relevant is found. The applicable code (old or new criminal codes) is decided by rules and stated. Quality is measured on a 200-question golden set and guarded by a CI gate (Phase 4). Every answer ends with "legal information, not legal advice". | Known weaknesses in `runlogs/phase4/RESULTS.md` (e.g. example leakage in 3/40 answers, all flagged by the verifier). The golden set was not reviewed by a lawyer. |
| **LLM08 Hidden Context Exposure** | The system prompt contains no secrets, keys or internal data: only answering rules and the retrieved passages, which the user sees as citations anyway. Secrets live in environment variables, never in prompts. | — |
| **LLM09 Vector and Embedding Weaknesses** | The index contains only manifest documents; users cannot add documents. Single tenant, no per-user data in the index. At start-up the API refuses an index built with a different embedding model, dimension, prefix or BM25 version (`api/container.py`). Filters are applied by Qdrant on payload fields, not by the model. | — |
| **LLM10 Improper Output Handling** | The frontend renders answers as Markdown with raw HTML disabled and links limited to http(s). Model output is never executed, used in a query or passed to a shell. SSE payloads are JSON-encoded. API responses carry `Content-Security-Policy: default-src 'none'`. | — (nginx serves the UI with `default-src 'self'`, no inline scripts; a Playwright test fails on any CSP violation against the real nginx in CI) |

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
| Secrets only in environment variables; `.env` is git-ignored | `backend/.env.example`, `deploy/.env.example` |
| Behind nginx: uvicorn trusts `X-Forwarded-For` (`--proxy-headers`) so rate limits see the client IP; only nginx can reach the API (no published port) | `backend/Dockerfile`, `deploy/compose.yaml` |

## Container and deployment controls

| Control | Where |
| --- | --- |
| API runs as uid 10001, read-only root filesystem, all capabilities dropped, `no-new-privileges`; writable only `/tmp` (tmpfs) and its var volume; corpus mounted read-only | `backend/Dockerfile`, `deploy/compose.yaml` |
| Web runs as nginx's unprivileged user on port 8080, read-only root; `/metrics` and the API docs are not exposed through it | `frontend/Dockerfile`, `frontend/nginx/default.conf.template` |
| Only the web port is published; Prometheus, Grafana bind to 127.0.0.1; Qdrant, Redis, Ollama only on the internal network | `deploy/compose.yaml` (tested) |
| No secrets in images or compose files: `deploy/.env` (git-ignored) holds the admin key and Grafana password; `.dockerignore` keeps `.env` files out of build contexts | `deploy/.env.example`, `backend/.dockerignore` |
| Deploys are smoke-tested through nginx and rolled back automatically; a pre-deploy backup of the index is taken | `deploy/deploy.py` |
| Index backups are checksummed (SHA-256) and verified before restore; 7 kept | `backend/src/lexai/ingestion/backup.py` |
| Logs rotate (10 MB × 5 per container) | `deploy/compose.yaml` |

TLS is not terminated by the stack: on a public server, put it behind a TLS-terminating
proxy or load balancer (Caddy, Traefik, a cloud LB) and keep port 8080 closed to the internet
(see the runbook).

## Checks

- `tests/api/test_v1.py`: forged sessions, 429 + `Retry-After`, 413, security headers, admin key, path traversal on PDFs, error shape.
- `tests/unit/test_state_and_telemetry.py`: session isolation and expiry (memory and Redis), PII masking on traces.
- `tests/unit/test_compose_config.py`: pinned images, hardened API container, only the web port public.
- CI: `pip-audit`, `npm audit --audit-level=high`, Trivy on both images, Playwright CSP test against nginx, deploy rehearsal with a planted bad release.
