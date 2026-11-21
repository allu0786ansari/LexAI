# Phase 6 — verified results

Two places, 21–22 Nov 2026:

- **CI** (GitHub Actions, a fresh Ubuntu runner every pull request): the `Docker` job of
  `ci.yml` — last full run green on commit `cc4c297`.
- **The target laptop**, logs in [`output/`](output/): run 4 (commit `cc4c297`) for every step
  except `07_browser.log` and `11_collect.log`, which come from the browser re-run (commit
  `9587e9d`). What each run found and fixed is in the [run history](#run-history).

**Laptop:** Windows (build 26300), 8 GB RAM, GTX 1650 4 GB, **Rancher Desktop** (moby, WSL2,
Docker 29.5, Compose v5.3), Ollama 0.40.16 on Windows, Python 3.12.4. **Stack:** the laptop
overlay — Qdrant, Redis, API and nginx in containers; Qwen2.5-7B-Instruct Q4_K_M (4K context)
and nomic-embed-text in Ollama on Windows; profile `laptop-8gb v4`.

## Exit criteria (plan, Phase 6)

| Criterion | Result | Evidence |
| --- | --- | --- |
| Fresh machine: clone → `.env` → up → ingest gives a working app, no manual steps beyond the model download | **Pass** | CI on every PR: build, start (models pulled by `ollama-init`), ingest, smoke test through nginx (5/5 answers grounded). Laptop: the same with the host's Ollama, after the one-time `ollama-for-docker.bat` (Windows-specific networking, below) |
| A planted bad release rolls back automatically | **Pass** | CI: release with a wrong model name never ready → previous release restored and re-tested (87 s). Laptop: the same, rolled back in 119 s; the API runs `lexai-backend:local` again (`10_rehearsal.log`) |
| Pushing a tag deploys to the server | **Built, not exercised** | `release.yml` publishes images to GHCR with SBOM and provenance and deploys over SSH with the same `deploy.py`. No server exists yet (decision at the start of Phase 6); the deploy job runs once `DEPLOY_ENABLED` and the secrets are set |
| Public HTTPS demo URL; README with architecture, evaluation table, demo GIF | **Not done** | Needs a server. README has the architecture and the evaluation table; TLS proxy setup is in the runbook |

## On the laptop (run 4 + browser re-run)

| Step | Result |
| --- | --- |
| Images | backend 436 MB, web 84 MB; run as uid 10001 and uid 101 (non-root) |
| Network | from a container, Windows' Ollama answers at 192.168.127.254 (Rancher's tunnel address, saved to `deploy\var\host.env`) |
| Models | both served, context pinned at 4096; first token 53 s including model load, then ~4.8 tokens/s, model 50% on the GPU |
| Index | live in the Qdrant container: 63 documents, **5,070 chunks** (same count as Phases 2–5) |
| **Deploy** (`deploy.py`, real model) | ready; UI served with its CSP; `/metrics` 404 through nginx; **5/5 answers cite sources** (25–45 s each); recorded as the current release; 203 s in total |
| Browser (Playwright, Edge, against nginx) | the real question passed (30 s); 19 of 20 mocked tests passed under the production CSP, including the CSP check on mobile. The desktop CSP test timed out waiting for its *mocked* answer while the laptop was generating — fixed by running tests one at a time when a live test runs (CI and the mobile run passed it) |
| Prometheus | API target up, `lexai_ready = 1`, requests counted, 5 alert rules loaded |
| Grafana | LexAI dashboard provisioned, data source healthy, metrics visible through Grafana |
| Backup / restore | 57.8 MB backup of 5,070 points → Qdrant volume deleted → empty server → restored and live → answers again; ~30 s for the whole cycle |
| Rehearsal | bad release detected ("model 'does-not-exist-in-this-release' is not served"), previous release restored and passed its smoke test; rolled back in 119 s |

Memory in the Linux VM while serving: API 146 MB, Qdrant 67 MB, Redis 5 MB, nginx 5 MB,
Prometheus 34 MB, Grafana 311 MB (`11_collect.log`). Ollama and the 7B model stay on Windows.

## In CI (every pull request)

Images build; Trivy: no critical, 1 high (reported); stack + Langfuse start from nothing with
`qwen2.5:0.5b-instruct` and a Constitution-only index; first release smoke-tested; 21
Playwright tests against nginx incl. one live question; Prometheus/Grafana checks;
**Langfuse received the traces** (9 traces, 43 observations: one per stage — condense,
retrieve, context, generate, verify); two API replicas both scraped and ready, sharing
sessions through Redis; backup → Qdrant volume deleted → restore → answers; planted bad
release rolled back.

## Not verified

- The GPU overlay (vLLM): checked as configuration only; no GPU available.
- Langfuse on the laptop: needs ~4 GB of its own; verified in CI.
- Deploy from a tag to a server, the public HTTPS URL and the demo GIF: no server yet.

## Run history

| Run | Found | Fixed |
| --- | --- | --- |
| 1 | Containers could not reach Ollama (Rancher Desktop maps `host.docker.internal` differently from Docker Desktop; Ollama listened on 127.0.0.1); port 8080 belongs to Apache, so every browser test hit Apache; `check_models.py` exited 0 on failure | `ollama-for-docker.bat`; `lexai.bat network` probes the host's addresses and saves a working one; laptop UI on 8090; exit code fixed; model steps skipped after a network failure |
| 2 | The probe found the right address (192.168.127.254) but `lexai.bat` lost its own path after `shift` | self path captured first |
| 3 | Network, models, ingest, backup/restore and the rollback mechanism passed; the API itself still could not reach Ollama — `deploy.py` recreated it without `host.env` | `deploy.py` reads `host.env` |
| 4 | All steps pass except one live browser test (two real answers generated at once on the laptop) | live test runs once (desktop); its error text and the API log are captured |
| re-run | Live test passes; one mocked test timed out under load | tests run serially with a live test; expect timeout 15 s |
