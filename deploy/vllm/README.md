# Serving Qwen2.5-7B-Instruct with vLLM (GPU)

Production serving uses vLLM with the 4-bit AWQ checkpoint. It exposes the same
OpenAI-compatible API as Ollama, under the same model name, so only
`LEXAI_LLM_BASE_URL` changes.

```bash
docker run --gpus all --rm -p 8001:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --served-model-name qwen2.5-7b-instruct \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.85
```

Then point the API at it:

```env
LEXAI_LLM_BASE_URL=http://localhost:8001/v1
LEXAI_LLM_API_KEY=EMPTY
```

Fits a 16 GB GPU (T4/L4 class). Phase 6 moves this into `deploy/compose.gpu.yaml`.
