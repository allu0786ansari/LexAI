# LexAI evaluation — commit `96a538c`

Date: 2026-11-21T09:36:34+00:00  
Dataset: `eval/datasets/golden_v1.jsonl`; every item is checked against the indexed source text before it is scored (items that fail are excluded and listed below).  
Base settings: `{"llm": "qwen2.5-7b-instruct", "prompt": "answer_v4", "embedder": "nomic-embed-text", "reranker": null, "mode": "hybrid", "routing": true, "hyde": false, "condense": true, "abstain_min_dense": 0.63, "context_char_budget": 6000, "rerank_top_n": 5}`

## Answers (dev, 40 items, judge: none)

| Metric | Value | n |
| --- | --- | --- |
| answered | 0.750 | 40 |
| verified (no flags) | 0.875 | 32 |
| citation coverage | 0.733 | 36 |
| citation validity | 1.000 | 32 |
| gold provision cited | 0.879 | 33 |
| key phrases present | 0.655 | 29 |
| correct code applied | 1.000 | 12 |
| injection resisted | 1.000 | 3 |
| abstention precision | 0.625 | |
| abstention recall | 1.000 | |
| time to first token (ms, mean) | 20757.846 | |
| total time (ms, mean) | 51209.188 | |
| prompt processing (tokens/s, mean) | 105.249 | |
| generation (tokens/s, mean) | 5.266 | |

Errors: 0.

Judged metrics (faithfulness, relevancy, context precision/recall, citation support) were not run: no judge model is configured. They need a model larger than the 7B generator (LEXAI_EVAL_JUDGE_BASE_URL / _MODEL / _API_KEY).

## Load (laptop, one process, embedded Qdrant)

| Run | Requests | Errors | p50 ms | p95 ms | max ms | req/s |
| --- | --- | --- | --- | --- | --- | --- |
| search c=1 | 48 | 0 | 272 | 461 | 523 | 3.53 |
| search c=4 | 48 | 0 | 935 | 1768 | 1876 | 4.27 |
| search c=8 | 48 | 0 | 1310 | 3510 | 3864 | 4.33 |
| answer total c=1 | 4 | 0 | 29088 | 40797 | 40797 | 0.04 |
| answer first token c=1 | 4 | 0 | 15392 | 25685 | 25685 | 0.04 |
| answer total c=2 | 4 | 0 | 131034 | 134117 | 134117 | 0.02 |
| answer first token c=2 | 4 | 0 | 34771 | 110189 | 110189 | 0.02 |

