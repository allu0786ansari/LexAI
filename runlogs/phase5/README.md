# Phase 5 — run and report (Windows Command Prompt)

Phase 5 turns the pipeline into a product someone else can run and debug: a versioned
API, a TypeScript frontend where every citation opens its source, security controls
mapped to the OWASP Top 10 for LLM Applications, and traces and metrics that show where
each second and each failure goes. Everything runs in **cmd.exe**. Run the steps below,
then push `runlogs\phase5\output\`.

## What changed for you

- **API v1** (`/api/v1/...`). The old `/api/query` is gone; the new UI and all scripts use v1.
- **Sessions are issued by the server.** A made-up or expired session id gets "404 session
  not found"; the UI then starts a new session by itself (B11).
- **Rate limit:** 20 questions a minute per computer and per conversation; the 21st gets
  "Try again in N s".
- **New UI** (TypeScript): click any `[n]` to open the passage — the whole section, its act,
  status and page — with a link to that page of the PDF. A badge shows whether every
  provision was checked. Stop, regenerate, copy, thumbs up/down, filters (acts, "in force
  only"), and on a phone a menu with the history and New chat (B13). The browser saves a
  conversation when an answer finishes, not on every word (B12).
- **Traces:** each question's path through the pipeline, with the time per stage, is
  written to `backend\var\traces.jsonl` (laptop profile v4). Show the last one with
  `.venv\Scripts\python.exe scripts\show_trace.py`.
- **Metrics** at http://localhost:8000/metrics (for Prometheus/Grafana in Phase 6).
- **Admin reindex** through the API (needs `LEXAI_API_ADMIN_API_KEY`); the check sets a
  temporary key itself.
- **Fixed:** `build --only <id>` used to publish an index with only that document; the
  others are now carried over.
- New Python packages (redis, limits, prometheus-client, opentelemetry); `slowapi` is
  removed. `setup` installs them.

## 1. Get the branch

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git fetch origin
git checkout phase-5-api-ui-security-observability
git pull
```

## 2. Run everything (about 20–25 minutes)

Close any LexAI API you started yourself, keep the laptop plugged in, then:

```bat
runlogs\phase5\run_phase5.bat
```

| Step | What it does | Log |
| --- | --- | --- |
| `check` | Git state, Python, Ollama, Node, GPU, RAM, port 8000 | `01_check.log` |
| `setup` | pip install (new packages); laptop profile **v4** (adds the trace file; the old `.env` is saved as `.env.previous`) | `02_setup.log` |
| `quality` | ruff, format check, mypy, tests (Redis tests skip on Windows; CI runs them) | `03_quality.log` |
| `frontend` | `npm ci`, type check, lint, unit tests, production build (skipped if Node.js is not installed) | `04_frontend.log` |
| `models` | Models present and responding | `05_models.log` |
| `phase5` | Sessions, security, every citation opens its source, feedback, metrics, a full trace, admin reindex, rate limit (~6–8 min) | `06_phase5.log`, `06_server.log` |
| `phase3` | The Phase 3 checks again, through API v1 (~5 min) | `07_phase3.log`, `07_server.log` |
| `smoke` | Streaming answers, two at once | `08_smoke.log`, `08_server.log` |

## 3. Send the results

```bat
git add runlogs\phase5\output
git commit -m "Phase 5 run logs"
git push
```

## What "pass" looks like

| Log | Pass |
| --- | --- |
| `03_quality.log` | every command `exit=0` |
| `04_frontend.log` | every command `exit=0`, or SKIPPED (no Node.js) |
| `06_phase5.log` | `SUMMARY: ALL PASSED` — forged session 404, 413, headers, admin 401, every citation and PDF opens, trace with all five stages, reindex succeeded with no document lost, 20 allowed then 429 |
| `07_phase3.log`, `08_smoke.log` | `SUMMARY: ALL PASSED` |

## Run 3 (after runs 1 and 2)

Only the parts that changed (about 10 minutes):

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git pull
runlogs\phase5\run_phase5.bat quality phase5
git add runlogs\phase5\output
git commit -m "Phase 5 run 3 logs"
git push
```

## Optional: try the new UI

With Node.js installed, in two cmd windows:

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI\backend
.venv\Scripts\python.exe -m uvicorn lexai.api.app:create_app --factory --port 8000
```

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI\frontend
npm run dev
```

Open http://localhost:5173, ask a question, click a `[1]`.

## Run history

| Run | Date | Result |
| --- | --- | --- |
| 1 | 2026-11-21 | Quality 187 passed, 4 skipped (Redis tests; CI runs them); frontend type check, lint, 13 unit tests, build all passed; Phase 3 checks and smoke **all passed through API v1**. Phase 5: 17/18 — forged session 404, 413, headers, admin 401, **15/15 citations and 15/15 PDF links open**, all three answers verified, metrics, a full trace with every stage, **reindex of prisons-act-1894 in 100 s with all 5,070 chunks kept and the API switched to the new index**. **Failed:** the rate-limit check — all 21 requests got 200. Not a limiter bug: each request took ~7.5 s because a refused question inside a conversation was still rewritten by the model before the refusal, so the 21 requests were spread over ~150 s and never 20 in one minute. **Fixed:** refusal is decided on the user's own words before any rewrite (no model call at all; test added); the check now sends the 21 requests at once. |
| 2 | 2026-11-21 | Quality 188 passed (incl. the new refusal test). Phase 5: the first 14 checks passed again (sessions, security, 15/15 citations and PDF links, three verified answers, feedback, metrics, full trace). Then **the API process died during the admin reindex**, about 15 s in, while carrying documents into the new build: no Python error or shutdown in its log, so it was killed or crashed natively; no exit code was recorded and the check script stopped with a traceback. The same reindex had passed in run 1 (100 s). **Found while investigating:** a build interrupted this way leaves a half-written collection that is newer than the live one, so the next build's clean-up kept it as the rollback copy and deleted the last good build (and in embedded mode it is loaded into memory at every start). **Fixed:** only complete builds are kept for rollback; leftovers of interrupted builds are removed at the start of every build (test added). **Diagnostics:** the API now runs with Python's faulthandler (native crashes print a stack trace into the server log); the check samples the API's memory during the reindex; if the API dies it reports the exit code and log tail, restarts the API, checks the live index is intact and answering, and retries the reindex once. Rehearsed by killing the API mid-reindex: index intact, answers served, retry succeeded and removed the half-written build. Reindex memory measured here: +70-80 MB. |

