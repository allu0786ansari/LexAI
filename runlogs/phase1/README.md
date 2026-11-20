# Phase 1 — run and report (Windows Command Prompt)

Everything here runs in **cmd.exe** with plain Python and pip — no uv, no
PowerShell. Run the steps below, then push `runlogs\phase1\output\` (or paste
the files in chat). Phase 2 starts only after they are reviewed.

## Your machine and the laptop profile

Your first run showed Windows, **8 GB RAM** and an NVIDIA GPU with **4 GB VRAM**.
Qwen2.5-7B (4-bit, ~4.7 GB) does not fit in 4 GB of VRAM, so Ollama runs part of
it on the GPU and the rest on the CPU. It works, but answers take longer to start.
To leave enough memory, setup writes `backend\.env` from
`deploy\env\laptop-8gb.env`. If you already have a `backend\.env` from the old
version (one without a `# lexai-profile:` line), setup saves it as
`backend\.env.before-phase1` (git ignores it) and replaces it. The profile:

- builds Qwen2.5-7B with a **4K context** (`deploy\ollama\Modelfile.laptop`)
- uses **nomic-embed-text** (already on your machine, ~0.3 GB) for embeddings
- turns the **reranker off** (it needs torch and ~2.5 GB RAM)
- sends 5 passages per question and allows up to 10 minutes for the first token

## 0. Before you start

1. **Python 3.12** must be installed (3.13 also works; 3.14 does not yet).
   Check in cmd: `py -0p`. If 3.12 is missing, install it from
   https://www.python.org/downloads/windows/ and tick **"Add python.exe to PATH"**.
2. **Ollama** must be running (its icon in the system tray).
3. **Internet for the model download.** Your last run failed with
   `lookup registry.ollama.ai: no such host`, which means DNS could not resolve
   Ollama's server. Check with `nslookup registry.ollama.ai`. If it fails, try
   another network (e.g. a phone hotspot), or set your DNS to `8.8.8.8`.
4. **Recommended:** keep the project outside OneDrive (e.g. `C:\dev\LexAI`).
   OneDrive syncing the virtual environment and the index slows everything and
   can lock files mid-write.
5. Close browsers and other heavy apps while the model runs. Your last check showed only
   ~0.9 GB of RAM free before the model loaded; 8 GB is tight.

## 1. Get the latest branch

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git checkout phase-1-foundation
git pull
```

## 2. Run everything

```bat
runlogs\phase1\run_phase1.bat
```

| Step | What it does | Log |
| --- | --- | --- |
| `check` | Windows version, Python versions, Ollama, DNS, GPU, RAM | `01_check.log` |
| `setup` | Creates `backend\.venv` with Python 3.12, installs pinned packages with pip, writes `backend\.env` (laptop profile) | `02_setup.log` |
| `quality` | ruff, format check, mypy, 51 tests | `03_quality.log` |
| `models` | Downloads Qwen2.5-7B (4.7 GB) if missing, builds `qwen2.5-7b-instruct` (4K context), checks the embedder, runs a short generation and reports tokens/second and how much of the model is on the GPU | `04_models.log` |
| `ingest` | Embeds the 59 PDFs (~2,600 chunks) into `backend\var\index`; takes several minutes | `05_ingest.log` |
| `smoke` | Starts the API, asks real questions, checks streaming, 2 parallel answers, disconnect handling, stops the API | `06_smoke.log`, `06_server.log` |

To repeat one step: `runlogs\phase1\run_phase1.bat smoke` (or `models`, `ingest`, ...).

## 3. Outage check

```bat
runlogs\phase1\run_phase1.bat outage
```

This **stops Ollama**, checks that LexAI reports the outage instead of
answering, then starts Ollama again. Log: `07_outage.log`.

## 4. Send the results

```bat
git add runlogs\phase1\output
git commit -m "Phase 1 run logs"
git push
```

## What "pass" looks like

- `03_quality.log`: ruff, format and mypy clean; `51 passed`.
- `04_models.log`: `SUMMARY: ALL PASSED`, `num_ctx=4096`, embedding dimension 768,
  and a line like `loaded: qwen2.5-7b-instruct ... NN% on GPU`.
- `05_ingest.log`: 59 files, about 2,608 chunks; law types `statute`, `case_law`, `manual` only.
- `06_smoke.log`: `SUMMARY: ALL PASSED`.
- `07_outage.log`: `SUMMARY: ALL PASSED` (readyz 503, error `upstream_unavailable`).

## If something fails

- **`check_models.py` shows the wrong models**: its first line prints the config file and
  profile. It must say `profile: laptop-8gb`. If not, run `run_phase1.bat setup` again.
- **`ERROR: The process "ollama.exe" not found` in the outage log** is harmless: stopping
  "ollama app.exe" with its child processes already stops the server.

- **`Python 3.12 or 3.13 was not found`**: install Python 3.12 (step 0.1), open a new cmd window, run `run_phase1.bat setup`.
- **Model download fails**: it is a network/DNS problem (step 0.3); fix it, then run `run_phase1.bat models`.
- **Very slow or "out of memory" with 7B**: switch to the 3B model you already have. Add this line to `backend\.env`, then re-run `smoke`:
  `LEXAI_LLM_MODEL=qwen2.5:3b-instruct`
  Report that you did; 7B stays the target for the GPU deployment in Phase 6.
- **Port 8000 busy**: close the other program, or run the smoke script directly:
  `cd backend` then `.venv\Scripts\python.exe scripts\smoke_phase1.py --start-server --port 8010`
