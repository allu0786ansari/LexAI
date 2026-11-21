# LexAI runbook

How to install, operate, upgrade and recover a LexAI deployment (Docker Compose on one
machine). Commands run from the repository root; `make` targets are for Linux/macOS, and
`deploy\lexai.bat` does the same on the Windows laptop.

## 1. What runs where

| Service | Image | Port | State |
| --- | --- | --- | --- |
| `web` | `lexai-web` (nginx, unprivileged) | **8080** (the only public port) | none |
| `api` | `lexai-backend` (FastAPI, 1 uvicorn worker per replica) | internal 8000 | `api-var` volume: feedback SQLite, reports |
| `qdrant` | `qdrant/qdrant:v1.20.2` | internal 6333 | `qdrant-data` (the index), `qdrant-snapshots` |
| `redis` | `redis:7.4.11-alpine` | internal 6379 | `redis-data`: sessions, rate limits, answer cache |
| `ollama` (+ `ollama-init`) | `ollama/ollama:0.40.16` | internal 11434 | `ollama-models` (~5 GB) |
| `vllm` (GPU overlay) | `vllm/vllm-openai:v0.34.0` | internal 8000 | `hf-cache` |
| `prometheus` | `prom/prometheus:v3.16.0` | 127.0.0.1:9095 | `prometheus-data` (15 days) |
| `grafana` | `grafana/grafana:13.3.1` | 127.0.0.1:3001 | `grafana-data` |
| Langfuse (separate project) | `deploy/langfuse/` | 3000 | its own volumes |
| jobs: `ingest`, `eval`, `smoke` | `lexai-backend` | — | bind mounts: `data/`, `eval/`, `deploy/var`, `deploy/backups` |

Requests: browser → nginx → `/api/*` and `/readyz` to an API replica (Docker DNS, round
robin) → Qdrant / Redis / model server. `/metrics` is not reachable through nginx;
Prometheus scrapes each replica directly.

**Sizing.** CPU-only server: 4 vCPU / 16 GB RAM runs the 7B model at a few tokens per
second (answers take ~1 min). GPU server: 1 × 16 GB GPU (T4/L4) with `compose.gpu.yaml`.
Disk: ~10 GB for images and models, ~1 GB for the index and backups.

## 2. First installation

### Linux server

```bash
git clone https://github.com/allu0786ansari/LexAI.git /opt/lexai && cd /opt/lexai
cp deploy/.env.example deploy/.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> LEXAI_API_ADMIN_API_KEY
$EDITOR deploy/.env                     # admin key, Grafana password; LEXAI_REGISTRY/TAG for releases
make up                                 # pulls images; ollama-init pulls/builds the models (first time ~10 min)
make docker-ingest                      # fetch + build the index; prints BUILD STATUS: live
make docker-status                      # the live index and its documents
make smoke-release                      # readiness + 5 questions through nginx
make up-observability                   # Prometheus + Grafana
make backup                             # first backup
```

Firewall: open only 443 (your TLS proxy) or 8080. LexAI does not terminate TLS; put
Caddy, Traefik or a cloud load balancer in front, e.g. Caddy: `lexai.example.org {
reverse_proxy 127.0.0.1:8080 }` with `LEXAI_WEB_BIND=127.0.0.1` in `deploy/.env`.
Keep the proxy's buffering off for `/api/v1/chat` (Server-Sent Events).

### Windows laptop (8 GB, Docker Desktop or Rancher Desktop)

Ollama keeps running on Windows with the models from the earlier phases; the containers
reach it at `host.docker.internal:11434`. On WSL2 engines (Rancher Desktop in particular)
that name points at the Windows side of the WSL network, not at 127.0.0.1, so Ollama must
listen on all addresses and the firewall must let the WSL network in:
`deploy\ollama-for-docker.bat` does both (the firewall part from an Administrator prompt).
With Rancher Desktop, turn Kubernetes off (Preferences → Kubernetes) — LexAI does not use it
and it costs memory. The web UI is on port 8090 on the laptop (8080 is often taken). Limit Docker's memory so the model keeps its
share: `%UserProfile%\.wslconfig`

```ini
[wsl2]
memory=3GB
swap=4GB
```

then `wsl --shutdown` and restart Docker Desktop.

```bat
deploy\ollama-for-docker.bat    REM once
deploy\lexai.bat build
deploy\lexai.bat up
deploy\lexai.bat network         REM containers can reach Ollama on Windows
deploy\lexai.bat models          REM the models answer
deploy\lexai.bat ingest          REM ~10 min; reuses data\raw downloads
deploy\lexai.bat smoke
start http://127.0.0.1:8090
```

If `network` still fails after `ollama-for-docker.bat`, the firewall rule is missing: run it
again from an Administrator prompt. `deploy\ollama-for-docker.bat undo` reverts both changes.

## 3. Releases and deploys

A release is a version tag. `release.yml` builds both images, pushes them to
`ghcr.io/allu0786ansari/lexai-{backend,web}:<version>` (and `:<commit sha>`) with an SBOM
and provenance attestation, and attaches the SBOMs and the latest evaluation report to the
GitHub release.

```bash
# bump the version in backend/pyproject.toml, frontend/package.json, then:
git tag v0.6.0 && git push origin v0.6.0
```

**Deploy** on the server (or automatically from `release.yml` when `DEPLOY_ENABLED=true`):

```bash
cd /opt/lexai && git fetch --tags && git checkout v0.6.0
python3 deploy/deploy.py --tag 0.6.0 --min-cited 4
```

`deploy.py` pulls the images, backs up the index, recreates `api` and `web`, and runs the
smoke test through nginx (readiness within 300 s, the UI with its CSP, a session, five
questions; with `--min-cited 4`, at least four answers must cite a source). If anything
fails it saves the container logs to `deploy/var/deploy-failure-*.log`, starts the
previous release again, smoke-tests it and exits 1. Releases are recorded in
`deploy/var/release.json`. Expect ~30 s of failed requests while the API restarts with a
single replica; with two or more, the window shrinks to the replicas' restart.

**Roll back by hand**: `python3 deploy/deploy.py --rollback` (or
`--tag <older version>`).

**Database migrations**: there are none; the only schema is the index, which is versioned
by its build metadata. A release that changes the embedding model, chunking or BM25 scheme
needs a new index: run `make docker-ingest` *before* switching (the build is blue/green:
the old index stays live until the new one passes its checks), or the API will refuse the
old index with `index_mismatch` and the deploy rolls back.

## 4. The index

| Task | Command |
| --- | --- |
| Rebuild everything | `make docker-ingest` |
| Re-embed only some documents (others copied) | `make docker-ingest ARGS="--only bns-2023"` |
| Rebuild from the running API | `curl -X POST -H "X-API-Key: $KEY" http://localhost:8080/api/v1/admin/reindex` |
| See what is live | `make docker-status` |
| Roll back to the previous build | it is kept (`LEXAI_QDRANT_KEEP_PREVIOUS=1`); restore a backup or rebuild |

Every replica checks the live alias on each readiness probe (every 30 s) and switches to a
new build by itself, whichever process published it.

### Backups

`make backup` (or `deploy\lexai.bat backup`) snapshots the live collection through Qdrant's
snapshot API into `deploy/backups/lexai-<UTC time>/` with its build metadata and a SHA-256
checksum; the 7 newest are kept. Schedule it daily:

```cron
17 2 * * *  cd /opt/lexai && make backup >> deploy/var/backup.log 2>&1
```

Copy `deploy/backups/` off the machine (object storage, another host). The corpus needs no
backup (bundled in git or re-downloadable and checksum-pinned). Back up the feedback
database too if you use it: `docker compose --project-directory deploy -f deploy/compose.yaml
cp api:/app/backend/var/feedback.sqlite3 deploy/backups/`.

### Restore

```bash
make restore                              # newest backup
make restore ARGS=lexai-20261121T021700Z  # a specific one
make docker-status && make smoke-release
```

The checksum is verified before anything changes; the snapshot is uploaded (re-creating the
collection on an empty server), its metadata written back and the live alias moved. CI
rehearses this on every pull request by deleting the Qdrant volume and restoring.

## 5. Monitoring

Grafana `http://127.0.0.1:3001` (admin / `GRAFANA_ADMIN_PASSWORD`) opens on the LexAI
dashboard: request rate and latency per route, time to first token, stage latencies,
answers by outcome, verification failures, cache hit rate, upstream errors, 429s, active
sessions, API memory. Alert rules (`deploy/prometheus/alerts.yml`) are visible at
`http://127.0.0.1:9095/alerts`; connect an Alertmanager to page someone.

| Alert | Meaning | First steps |
| --- | --- | --- |
| `LexAIDown` | Prometheus cannot scrape a replica for 2 min | `make ps`; `make logs ARGS=api`; a crash loop after a deploy → `python3 deploy/deploy.py --rollback` |
| `LexAINotReady` | a model server or the index is not ready | `curl -s localhost:8080/readyz \| python3 -m json.tool` names the failing part: `llm`/`embedder` → `make logs ARGS=ollama`, model missing → `docker compose ... restart ollama-init`; `index` → `make docker-status`, restore or rebuild |
| `LexAISlowFirstToken` | p95 time to first token above target for 10 min | model offloaded to CPU or overloaded: check GPU memory (`nvidia-smi`), Ollama logs, concurrent users; scale with a GPU or reduce `LEXAI_LLM_MAX_TOKENS` / context budget |
| `LexAIHighErrorRate` | more than 2% 5xx for 5 min | `make logs ARGS=api` and search the request ids; `lexai_upstream_errors_total` on the dashboard shows which dependency |
| `LexAIVerificationFailures` | many answers fail the citation check | a model or prompt change? compare with the last evaluation report; roll back the release |

**Traces**: with Langfuse (`deploy/langfuse/`, ~4 GB RAM) each request is a trace with
spans for condense, retrieve, context, generate and verify; find it by the `X-Request-ID`
a user reports. Without Langfuse, set `LEXAI_OBS_TRACE_FILE=var/traces.jsonl` and read it
with `docker compose ... exec api python scripts/show_trace.py --list`.

## 6. Scaling

- **More API replicas**: `LEXAI_API_REPLICAS=2` in `deploy/.env`, then `make up`. Sessions,
  rate limits and the cache are in Redis, so any replica can serve any request; nginx
  re-resolves the replicas every 10 s and Prometheus discovers them by DNS.
- **The model is the bottleneck**: one 7B model on one GPU serves a few concurrent answers;
  further requests queue in the model server. Use vLLM (`make up-gpu`), which batches
  concurrent requests, and a bigger or second GPU before adding API replicas.
- Qdrant and Redis are single instances; at this corpus size (~10k passages) they are far
  from their limits.

## 7. Routine maintenance

| What | How |
| --- | --- |
| Rotate the admin key | edit `LEXAI_API_ADMIN_API_KEY` in `deploy/.env`, `make up` (recreates the API) |
| Update a dependency image (Qdrant, Redis, …) | change the tag in `deploy/compose.yaml`, run CI, `make backup`, `make up` |
| Disk | `docker system df`; logs rotate at 10 MB × 5 per container; old images: `docker image prune` |
| Model update | change the Modelfile or `LEXAI_LLM_MODEL`, re-run `ollama-init`, run the evaluation (`make docker-eval ARGS="answers --limit 40"`) before deploying |
| Corpus update | edit `data/manifest.yaml`, `make docker-ingest ARGS="--only <id>"`, then `make docker-eval ARGS=validate` |

## 8. Disaster recovery

| Lost | Recovery | Time (CPU server) |
| --- | --- | --- |
| API / web containers | `make up` (stateless) | 1 min |
| Redis | starts empty: users get "session expired" and continue in a new session | 1 min |
| Qdrant volume | `make up && make restore` | 2-5 min |
| No backup either | `make docker-ingest` | 10-30 min |
| The whole machine | new host: section 2 with the same `deploy/.env`, then `make restore` with the copied backups | 20-40 min (model download) |

Recovery point: the last backup (daily) or a rebuild from the pinned corpus (no data loss;
the corpus is the source of truth).
