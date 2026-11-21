# ADR 0006 — Phase 6 containers, CI/CD, deployment and recovery

Date: 2026-11-21 · Status: accepted (verified in CI and on the target laptop, see runlogs/phase6/RESULTS.md)

## Context

After Phase 5 LexAI ran from a virtual environment on one laptop, with embedded Qdrant
(one process at a time) and in-process state. Nothing could be handed to someone else to
run, upgraded without downtime risk, or recovered after a lost disk. There is no server
yet: the deployment has to be proven without one and be ready for a VM later.

## Decisions

1. **Docker Compose on one machine, not Kubernetes.** The workload is one model server
   and a small API; Compose covers replicas, health checks, restarts and volumes with far
   less to operate. The images and the stateless API would move to Kubernetes unchanged.
2. **Two images, built for production.** `lexai-backend`: multi-stage uv build on
   `python:3.12.16-slim`, no build tools at runtime, uid 10001, read-only root, one uvicorn
   worker per container (metrics, the readiness probe and caches are per process; scale
   with replicas, not workers). `lexai-web`: Vite build served by unprivileged nginx with
   the production CSP, SSE-safe proxying (no buffering, 15-minute read timeout) and
   `/metrics` blocked. Models and the corpus are never baked in.
3. **Same configuration that was evaluated.** `deploy/.env.example` reproduces the Phase 4
   measurement (nomic-embed-text with prefixes, Qwen2.5-7B, no reranker, 1,400-character
   chunks, abstention 0.63); a test fails if it drifts from the laptop profile. The
   unevaluated server defaults stay documented in `backend/.env.example` only.
4. **Overlays instead of copies.** `compose.laptop.yaml` uses the host's Ollama and the
   8 GB profile (the laptop cannot fit a second model server); `compose.gpu.yaml` adds
   vLLM (AWQ, 8K context) and keeps Ollama for embeddings. Profiles keep optional pieces
   (Ollama, Prometheus/Grafana, jobs) out of the default start.
5. **Replicas find a new index themselves.** The readiness probe also checks the live
   alias, so an index published by the ingest job or by another replica is picked up within
   one probe interval; a first build may be partial (CI builds the Constitution only).
6. **Deploys are smoke-tested and roll back automatically** (`deploy/deploy.py`, Python so
   it runs on the Windows laptop and Linux servers alike): pull → back up the index →
   recreate api/web → readiness + UI/CSP + five questions through nginx → keep, or restore
   the previous release, re-test it and fail. CI rehearses this with a planted bad release
   (healthy process, wrong model name → never ready) and asserts the rollback.
7. **Backups through Qdrant's snapshot API**, not volume copies: consistent while serving,
   restorable onto an empty server, checksummed, with the build metadata restored too.
   CI deletes the Qdrant volume and restores on every pull request.
8. **Release pipeline**: a version tag publishes both images to GHCR (version + SHA tags),
   with SBOM and provenance; deploy runs from CI only when a server is configured
   (`DEPLOY_ENABLED` + environment secrets), behind the `production` environment's rules.
   The nightly full evaluation needs a GPU runner and is skipped cleanly without one.
9. **Fresh-machine test in CI**: build images, Trivy (fail on fixable critical), start the
   whole stack plus Langfuse with a tiny model (`qwen2.5:0.5b-instruct`) and a one-document
   index, then check answers through nginx, Playwright against nginx, metrics in Prometheus
   and Grafana, traces in Langfuse, two replicas sharing Redis, backup/restore, rollback.

## Consequences

- The quality numbers come from the laptop and the evaluation pipeline, not from CI: the
  tiny CI model proves the plumbing, not answer quality (`--min-cited 4` is used with the
  real model).
- No TLS inside the stack; a TLS proxy in front is required on a public host (runbook).
- No admission control yet: under load, answers queue in the model server.
- Image and model versions are pinned by tag, not digest.
- Langfuse needs ~4 GB of RAM, so on the 8 GB laptop it is verified in CI rather than locally.
- Container networking to the host differs by engine: Docker Desktop resolves
  `host.docker.internal` itself; Rancher Desktop reaches Windows through its tunnel address,
  and Docker's `host-gateway` is the Linux VM. `lexai.bat network` detects the working address
  and saves it (`LEXAI_HOST_GATEWAY`), and Ollama must listen beyond 127.0.0.1.
