#!/bin/sh
# Pulls the embedding model and builds the generator into the Ollama volume (first start only:
# later runs find the models and finish in seconds). Runs as the one-off `ollama-init` service.
set -eu
: "${OLLAMA_HOST:?}" "${EMBED_MODEL:?}"
LLM_MODEL="${LLM_MODEL:-}"
echo "embedding model: $EMBED_MODEL"
ollama pull "$EMBED_MODEL"
if [ -z "$LLM_MODEL" ]; then
  echo "generator: served elsewhere"
elif [ -n "${LLM_MODELFILE:-}" ]; then
  base=$(awk '/^FROM /{print $2; exit}' "$LLM_MODELFILE")
  echo "generator: $LLM_MODEL from $base ($LLM_MODELFILE)"
  ollama pull "$base"
  ollama create "$LLM_MODEL" -f "$LLM_MODELFILE"
else
  echo "generator: $LLM_MODEL"
  ollama pull "$LLM_MODEL"
fi
ollama list
