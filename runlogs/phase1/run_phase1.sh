#!/usr/bin/env bash
# Phase 1 verification for Linux / macOS / WSL (uses uv). On Windows use run_phase1.bat.
#
# Run from the repository root:
#   bash runlogs/phase1/run_phase1.sh            # every step
#   bash runlogs/phase1/run_phase1.sh smoke      # one step: env install quality models ingest smoke
#
# Every step writes runlogs/phase1/output/<step>.log. Send those files back
# (or commit and push them) for review.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/runlogs/phase1/output"
PORT="${LEXAI_PORT:-8000}"
CONCURRENCY="${SMOKE_CONCURRENCY:-2}"   # parallel questions in the smoke test
mkdir -p "$OUT"
cd "$ROOT/backend"

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
export LEXAI_API_ENVIRONMENT=ci          # JSON logs, easier to review

log_cmd() {  # log_cmd <logfile> <command...>
  local file="$1"; shift
  echo "\$ $*" | tee -a "$file"
  "$@" 2>&1 | tee -a "$file"
  local status=${PIPESTATUS[0]}
  echo "exit=$status" | tee -a "$file"
  echo | tee -a "$file"
}

step_env() {
  local f="$OUT/01_environment.log"; : > "$f"
  log_cmd "$f" git -C "$ROOT" rev-parse --abbrev-ref HEAD
  log_cmd "$f" git -C "$ROOT" rev-parse HEAD
  log_cmd "$f" uname -a
  log_cmd "$f" uv --version
  log_cmd "$f" uv run --no-sync python --version
  log_cmd "$f" ollama --version
  if command -v nvidia-smi >/dev/null 2>&1; then log_cmd "$f" nvidia-smi; else echo "nvidia-smi: not found (CPU only?)" | tee -a "$f"; fi
}

step_install() {
  local f="$OUT/02_install.log"; : > "$f"
  log_cmd "$f" uv sync --extra rerank
}

step_quality() {
  local f="$OUT/03_quality.log"; : > "$f"
  log_cmd "$f" uv run ruff check .
  log_cmd "$f" uv run ruff format --check .
  log_cmd "$f" uv run mypy
  log_cmd "$f" uv run pytest -v
}

step_models() {
  local f="$OUT/04_models.log"; : > "$f"
  # Downloads print progress bars, so they go to the terminal only.
  ollama pull qwen2.5:7b-instruct-q4_K_M
  ollama pull qwen3-embedding:0.6b
  log_cmd "$f" ollama create qwen2.5-7b-instruct -f "$ROOT/deploy/ollama/Modelfile"
  log_cmd "$f" ollama list
  log_cmd "$f" ollama show qwen2.5-7b-instruct
  log_cmd "$f" curl -s http://localhost:11434/v1/models
  log_cmd "$f" uv run python scripts/check_models.py
}

step_ingest() {
  local f="$OUT/05_ingest.log"; : > "$f"
  log_cmd "$f" uv run lexai-ingest
}

step_smoke() {
  local f="$OUT/06_smoke.log"; : > "$f"
  log_cmd "$f" uv run python scripts/smoke_phase1.py --start-server --port "$PORT" \
    --concurrency "$CONCURRENCY" --server-log "$OUT/06_server.log"
}

run_step() {
  case "$1" in
    env) step_env ;; install) step_install ;; quality) step_quality ;;
    models) step_models ;; ingest) step_ingest ;; smoke) step_smoke ;;
    *) echo "unknown step: $1 (env install quality models ingest smoke)"; exit 2 ;;
  esac
}

if [ $# -eq 0 ]; then
  for s in env install quality models ingest smoke; do echo "########## $s ##########"; run_step "$s"; done
else
  for s in "$@"; do run_step "$s"; done
fi

echo
echo "Done. Logs are in runlogs/phase1/output/. Next: the outage check in runlogs/phase1/README.md (step 7)."
