# Phase 5 — verified results

Three runs on the target laptop, 21 Nov 2026, logs in [`output/`](output/): `03_quality.log`
and `06_phase5.log` from run 3 (commit `7cce360`); `04_frontend.log`, `07_phase3.log` and
`08_smoke.log` from run 1 (commit `d1af790`; later changes did not touch those paths). What
each run found and fixed is in the [run history](README.md#run-history).

**Machine:** Windows (build 26300), 8 GB RAM, NVIDIA GTX 1650 4 GB, Python 3.12.4, Ollama
0.40.9, Node.js present. **Profile:** `laptop-8gb v4` — Qwen2.5-7B-Instruct Q4_K_M (4K
context), nomic-embed-text, no reranker, embedded Qdrant, in-process sessions and rate
limits, traces to `backend\var\traces.jsonl`. Index: 63 documents, 5,070 chunks.

## Exit criteria (plan, Phase 5)

| Criterion | Result | Evidence |
| --- | --- | --- |
| Every citation in the UI opens its source passage | **Pass** | API: 15/15 citations from three real answers open their passage, 15/15 open their PDF page (`06_phase5.log`). UI: Playwright clicks every `[n]` and the source list on desktop and phone (CI) |
| A forged session id is rejected | **Pass** | 404 `session_not_found` |
| The 21st request in a minute gets a 429 | **Pass** | 20 × 200, then 429 with `Retry-After: 60` |
| One question can be followed from query to verified answer, with time per stage | **Pass** (local trace file) | The follow-up "Is it bailable?" traced below. The same spans export to Langfuse over OTLP when configured; a Langfuse instance needs the Phase 6 stack |
| The Grafana dashboard shows live latency and error panels during a load run | **Deferred to Phase 6** | `/metrics` serves every series the dashboard and alerts query (checked by tests; promtool validates the rules in CI). Prometheus and Grafana need the Compose stack: no Docker on the laptop, and too little free RAM beside Ollama |

## Also verified on the laptop

| Check | Result |
| --- | --- |
| Lint, types, tests | 189 passed, 4 skipped (the Redis tests; CI runs them against a real Redis) |
| Frontend | type check, lint, 13 unit tests, production build |
| Security controls | 20 KB body → 413; security headers + CSP; admin endpoints 401 without or with a wrong key |
| Answers | three real answers, all passed verification; feedback stored |
| Metrics | answers, time to first token, stage latency; labels are route templates, never raw paths |
| Admin reindex (in-process) | `prisons-act-1894` rebuilt and 62 other documents carried over in 95 s; **5,070/5,070 chunks kept**; the API switched to the new index without a restart. It also removed the half-written build that run 2's crash had left |
| Phase 3 checks through API v1 | all passed: correspondence 44/45, exact lookups 4/4, mappings 2/2, BNS before IPC, follow-ups, abstention, verification, refusal without generation, cache hit in 0.01 s |
| Smoke | streaming, citations, the repealed IPC flagged, 2 concurrent answers, `/healthz` p95 7.7 ms while generating (495 probes), healthy after a client disconnect |

### One question, traced (`06_phase5.log`)

```
POST /api/v1/chat                               29764.0 ms
  lexai.chat                                    29764.0 ms   intent concept, new codes apply, answered, answer_v4
    lexai.condense                               5520.4 ms   rewrite rejected → "What is the punishment for murder? Is it bailable?"
    lexai.retrieve                               1396.3 ms   BNS s.103, IPC s.302, BNS s.105, IPC s.304, BNS s.104, IPC s.303, BNSS Schedule First
    lexai.context                                  15.8 ms   5 passages, 2,175 characters
    lexai.generate                              22788.6 ms   first token at 17.4 s; 1,518 prompt + 66 answer tokens
    lexai.verify                                    1.6 ms   valid; 50% of sentences cited
```

What the trace shows: generation is 77% of the time, and most of it is spent before the
first token (processing a 1,500-token prompt on a half-GPU 7B model). The rewrite costs
5.5 s per follow-up. And the passage that actually answers bailability (BNSS First
Schedule) ranked 7th, outside the 5 passages sent to the model — the bailability weak spot
seen in Phase 3 is still open.

## Found and fixed during the runs

1. **A refused question inside a conversation still cost a model call** (run 1): the
   follow-up rewrite ran before the refusal rules. Refusal is now decided on the user's own
   words first; crime-assistance requests never reach the model.
2. **An interrupted reindex could become the rollback copy** (run 2): the API died during a
   reindex, leaving a half-written collection newer than the live one, which the next
   build's clean-up would have kept for rollback while deleting the last good build. Only
   complete builds are kept now, and leftovers are removed when a build starts — confirmed in
   run 3, which removed the build left by run 2's crash.
3. **`build --only` dropped every other document** (found in development): partial builds
   now carry the other documents over.

The cause of run 2's crash is unknown: the process ended with no Python error. The API now
runs with Python's faulthandler (a native crash prints a stack trace to its log), reports
its own memory (`lexai_process_memory_bytes`), and the check restarts the API after a crash,
verifies the index is intact and retries — rehearsed by killing the API mid-reindex. Run 3
did not crash. The memory reading in run 3's log ("1 MB") measured the venv's launcher
process, not the interpreter; the check now reads the API's own metric.

## Carried to Phase 6

1. Prometheus + Grafana + Langfuse + Redis in the Compose stack; the live-dashboard criterion.
2. Several API workers (sessions and limits in Redis, already supported and tested).
3. Admission control for concurrent generation; a Qdrant server instead of the embedded store.
4. Bailability: the First Schedule tables need to reach the context for "is it bailable?".
