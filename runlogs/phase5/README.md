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
