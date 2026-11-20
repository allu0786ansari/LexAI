# Phase 1 — verified results

Run 2 on the target laptop, 20 Nov 2026, commit `1e6fffd`, logs in [`output/`](output/).
Reviewed against the Phase 1 exit criteria in the implementation plan.

**Machine:** Windows (build 26300), 8 GB RAM (~1.4 GB free at start), NVIDIA GTX 1650 4 GB, Python 3.12.4, Ollama 0.40.9.
**Profile:** `laptop-8gb` — Qwen2.5-7B-Instruct Q4_K_M (4K context), nomic-embed-text, no reranker.

## Exit criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Lint, format, types and tests clean | Pass | ruff, mypy clean; 51 passed on Windows (`03_quality.log`) |
| No LangChain or Google imports | Pass | removed from code and requirements |
| First token reaches the client before generation ends | Pass | 97 token events from 32.0 s to 52.8 s (`06_smoke.log`) |
| API stays responsive while answers stream | Pass | `/healthz` p50 3.8 ms, p95 8.2 ms over 711 probes during 2 parallel answers |
| Model server down → `/readyz` red, chat returns `error` | Pass | HTTP 503; `upstream_unavailable` with request id (`07_outage.log`) |
| Index records embedding model; ingestion swaps safely | Pass | 59 files → 2,608 chunks in 290 s; 0 files labelled `other` (`05_ingest.log`) |
| Same code path on Ollama and vLLM | Deferred | Needs a GPU server; verified in Phase 6 |

## Measured on this laptop

| Measure | Value |
| --- | --- |
| Qwen2.5-7B split | 5.2 GB loaded, 50% on GPU |
| Generation speed | ~5–6 tokens/s |
| Time to first token, short prompt (model load included) | 20 s |
| Time to first token, RAG prompt (5 passages) | 32 s |
| Retrieval latency | 196 ms |
| Ingestion (2,608 chunks, nomic-embed-text) | 290 s |

## Findings carried into Phase 2

1. **The IPC PDF in `backend/Data` is incomplete.** `IPC_1860.pdf` has 58 pages
   and never mentions sections 299, 300, 302 or 420, so "punishment for murder"
   cannot be answered from it. The model correctly said the passages did not
   contain the punishment instead of inventing it. Phase 2 replaces the corpus
   with complete official texts (India Code) via the manifest, and adds BNS,
   BNSS and BSA.
2. **Long time to first token (32 s)** comes from processing ~2,000 prompt tokens
   with half the model on the CPU. Section-level chunks in Phase 2 are shorter
   and more precise, which shrinks the prompt.
3. **Answer formatting:** the first answer began with a stray `[1]`. Phase 3's
   citation verifier checks and cleans citation markers.
4. Without retrieval, the model said an FIR is registered under "Section 154 of
   the Indian Penal Code" (it is CrPC s.154, now BNSS s.173) — the kind of error
   grounded, verified citations are meant to prevent.
