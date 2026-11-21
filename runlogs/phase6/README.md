# Phase 6 — run and report (Windows Command Prompt + Docker Desktop)

Phase 6 packages LexAI as containers that anyone can start with one command, and adds the
operations around them: deploys that test themselves and roll back, index backups and
restore, metrics in Prometheus and Grafana, and a CI pipeline that builds, scans and
deploys the whole stack on a fresh machine for every pull request. On the laptop, Ollama
keeps running on Windows (your Phase 2 models); Qdrant, Redis, the API and the web UI run
in Docker.

## What changed for you

- **`deploy\lexai.bat`** runs the stack: `build`, `up`, `ingest`, `smoke`, `deploy`,
  `rollback`, `backup`, `restore`, `observability`, `ps`, `logs`, `down`.
  The web UI is at **http://127.0.0.1:8080** (nginx in front of the API).
- The index now lives in the **Qdrant container** (volume `lexai_qdrant-data`), built once
  with `deploy\lexai.bat ingest`. Your old embedded index in `backend\var\qdrant` is not
  touched and still works without Docker (`run_phase5.bat` style).
- `deploy/.env.example` moved to `backend/.env.example` (the full settings reference);
  `deploy/.env.example` is now the Docker Compose configuration.
- Grafana http://127.0.0.1:3001 (admin / `lexai-laptop`), Prometheus http://127.0.0.1:9095.
- Langfuse needs about 4 GB of RAM on its own, too much next to the 7B model on 8 GB; CI
  runs it and checks that traces arrive. Traces on the laptop still go to
  `var/traces.jsonl` (now inside the API container's volume).

## 0. One-time: limit Docker's memory

Docker Desktop's Linux VM may otherwise take up to half the RAM, leaving too little for
the 7B model. Create `%UserProfile%\.wslconfig` (Notepad) with:

```ini
[wsl2]
memory=3GB
swap=4GB
```

Then in cmd: `wsl --shutdown`, and start Docker Desktop again (wait for "Engine running").

## 1. Get the branch

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git fetch origin
git checkout phase-6-deploy
git pull
```

## 2. Run everything (about 60-75 minutes)

Docker Desktop and Ollama must be running (the script starts Ollama if needed).

```bat
runlogs\phase6\run_phase6.bat
```

| Step | What it checks |
| --- | --- |
| `check` | Docker, Compose, Ollama, memory, `.wslconfig` |
| `build` | both images build; they run as non-root users |
| `up` | Qdrant, Redis, API, web start; `/metrics` is not reachable through nginx |
| `models` | the containers reach Ollama on Windows and the models answer |
| `ingest` | the index is built in the Qdrant container (~10 min) |
| `deploy` | first release via `deploy.py`: readiness, UI with CSP, five real answers, ≥ 4 citing a source |
| `browser` | Playwright in Microsoft Edge against nginx: the UI works under the production CSP, one real question |
| `observability` | Prometheus scrapes the API and loads the alerts; Grafana's dashboard and data source work |
| `backup` | backup → delete the index volume → restore → answers again |
| `rehearsal` | a planted bad release is detected and rolled back automatically |
| `collect` | container status, memory per container, logs |

One step only: `runlogs\phase6\run_phase6.bat deploy`. The stack keeps running afterwards;
stop it with `deploy\lexai.bat down` (the index stays in its volume).

## 3. Send the results

```bat
git add runlogs\phase6\output
git commit -m "Phase 6 run logs"
git push
```

## If something fails

- **`models` cannot reach Ollama**: quit Ollama from the tray, run
  `setx OLLAMA_HOST 0.0.0.0`, start Ollama again, allow it in Windows Firewall
  (private networks), then `runlogs\phase6\run_phase6.bat models`.
- **Docker "port is already allocated" for 8080**: another program uses the port; close it
  or set `LEXAI_WEB_PORT=8081` in `deploy\env\laptop-docker.env`.
- **The laptop becomes very slow**: check `.wslconfig` (step 0); close the browser tabs
  you don't need while answers are generated.
- **Build fails downloading packages**: re-run `runlogs\phase6\run_phase6.bat build`
  (downloads resume from Docker's cache).
