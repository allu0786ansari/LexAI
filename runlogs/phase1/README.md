# Phase 1 — run and report

Run these on your machine, then send back the files in `runlogs/phase1/output/`
(commit and push them on this branch, or paste them in chat). Phase 2 starts
only after these are reviewed.

## 0. Prerequisites

| Tool | Check | Install |
| --- | --- | --- |
| git | `git --version` | — |
| uv | `uv --version` | https://docs.astral.sh/uv/getting-started/installation/ |
| Ollama (running) | `ollama --version` | https://ollama.com/download |
| Disk | ~12 GB free | Qwen2.5-7B Q4 (~4.7 GB), Qwen3-Embedding-0.6B, reranker (~2.3 GB), Python deps |

uv installs Python 3.12 by itself if needed. A GPU with 8 GB+ VRAM is
recommended; CPU-only works but is slow (see the tips at the end).

## 1. Get the branch

```bash
git fetch origin
git checkout phase-1-foundation
git pull
```

## 2–6. Run everything

**Windows (PowerShell), from the repo root:**

```powershell
powershell -ExecutionPolicy Bypass -File runlogs\phase1\run_phase1.ps1
```

**Linux / macOS / WSL / Git Bash, from the repo root:**

```bash
bash runlogs/phase1/run_phase1.sh
```

The scripts run these steps in order; each writes its own log:

| Step | What it does | Log |
| --- | --- | --- |
| `env` | Records branch, commit, OS, uv, Python, Ollama, GPU | `01_environment.log` |
| `install` | `uv sync --extra rerank` (creates `backend/.venv`) | `02_install.log` |
| `quality` | ruff, format check, mypy, 47 pytest tests | `03_quality.log` |
| `models` | Pulls Qwen2.5-7B (Q4) and Qwen3-Embedding-0.6B, builds `qwen2.5-7b-instruct` from `deploy/ollama/Modelfile`, checks the embedding size is 1024 | `04_models.log` |
| `ingest` | Embeds the 59 PDFs into `backend/var/index` | `05_ingest.log` |
| `smoke` | Starts the API, runs `backend/scripts/smoke_phase1.py`, stops the API | `06_smoke.log`, `06_server.log` |

To re-run a single step: add `-Steps smoke` (PowerShell) or `smoke` (bash).

## 7. Outage check (manual, about 2 minutes)

This proves the API fails loudly when the model server is down.

1. Start the API in one terminal:
   ```bash
   cd backend
   uv run uvicorn lexai.api.app:create_app --factory --port 8000
   ```
2. **Stop Ollama.** Windows/macOS: quit Ollama from the tray / menu bar. Linux: `sudo systemctl stop ollama`.
3. In a second terminal, from `backend/`:
   ```bash
   uv run python scripts/smoke_phase1.py --expect-down > ../runlogs/phase1/output/07_outage.log 2>&1
   ```
   (PowerShell: same command, then `Get-Content ..\runlogs\phase1\output\07_outage.log`.)
4. Start Ollama again; stop the API with Ctrl+C.

## 8. Optional: try the UI

With Ollama and the API running (step 7.1):

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and ask a question. Answers now cite passages as
`[1]`, `[2]`… and stream word by word. The frontend itself changes in Phase 5.

## 9. What to send back

- All files in `runlogs/phase1/output/`
- Your hardware: GPU model and VRAM (or "CPU only") and RAM
- Anything that looked wrong, even if the scripts said PASS

## What "pass" looks like

- `03_quality.log`: ruff, format and mypy clean; `47 passed`.
- `04_models.log`: `qwen2.5-7b-instruct` listed, `num_ctx 8192` in its parameters, `embedding dimension: 1024`.
- `05_ingest.log`: about 2,600 chunks from 59 files; law types `statute`, `case_law`, `manual` (no `other`).
- `06_smoke.log`: `SUMMARY: ALL PASSED`; the token spread shows tokens arriving over several seconds.
- `07_outage.log`: `SUMMARY: ALL PASSED` (readyz 503, error event `upstream_unavailable`).

## Tips if something fails

- **CPU only / slow machine:** set a longer timeout and fewer parallel questions before the smoke step:
  - PowerShell: `$env:LEXAI_LLM_READ_TIMEOUT_S="600"`, then run with `-Steps smoke -Concurrency 2`
  - bash: `LEXAI_LLM_READ_TIMEOUT_S=600 SMOKE_CONCURRENCY=2 bash runlogs/phase1/run_phase1.sh smoke`
- **Reranker too heavy:** set `LEXAI_RERANK_ENABLED=false` and re-run `smoke`. Say so in your report.
- **Port 8000 busy:** `-Port 8010` (PowerShell) or `LEXAI_PORT=8010` (bash).
- **`readyz` says model not served:** re-run the `models` step and check `ollama list`.
