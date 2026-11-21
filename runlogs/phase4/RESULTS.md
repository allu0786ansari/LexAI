# Phase 4 — verified results

Three runs on the target laptop, 20–21 Nov 2026, logs in [`output/`](output/) (run 3)
and results per commit in [`eval/reports/`](../../eval/reports/). What each run found
and fixed is in the [run history](README.md#run-history).

**Machine:** Windows (build 26300), 8 GB RAM, NVIDIA GTX 1650 4 GB, Python 3.12.4, Ollama 0.40.9.
**Profile:** `laptop-8gb v3` — Qwen2.5-7B-Instruct Q4_K_M (4K context, 50% on GPU),
nomic-embed-text, no reranker, 6,000-character passage budget, abstention threshold 0.63.
Index from Phase 2 (63 documents, 5,070 chunks). Answer prompt `answer_v4`.

| Results | Commit | Contents |
| --- | --- | --- |
| [`da6e99f`](../../eval/reports/da6e99f/report.md) | run 1 | validation, retrieval ablations incl. HyDE, first test-split run, answers with `answer_v3` |
| [`82eeeaf`](../../eval/reports/82eeeaf/) | run 2 | retrieval after the follow-up fix (dev, 4 configs; test) |
| [`96a538c`](../../eval/reports/96a538c/report.md) | run 3 | answers with `answer_v4`, load test |

Retrieval code is the same in `82eeeaf` and `96a538c` (the later commit only changed the
evaluation runner and the run script).

## Exit criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Golden set covering every question type, checked against the corpus | Pass | 200 items, 7 categories; **200/200 valid** against the live index (`05_validate.log`) |
| Held-out test split that cannot be tuned on | Pass | 87 items, checksum-locked (`eval/datasets/golden_v1.lock.json`); the loader refuses an edited test item |
| Retrieval metrics per configuration and category | Pass | Tables below |
| Answer metrics (grounding, citations, abstention, refusal, injection) | Pass | Tables below; judged metrics not run (no judge model larger than 7B available) |
| Ablations show what each component is worth | Pass | Routing +0.22 MRR; hybrid vs dense/BM25; HyDE not worth its cost |
| Regression gate in CI without model servers | Pass | Fixture (12 MB) + baseline; GitHub Actions passes; routing off fails the gate (`09_fixture.log`, test `test_offline_gate_against_the_committed_baseline`) |
| Gate reproducible across machines | Pass | Linux reproduces the Windows numbers exactly; retrieval settings pinned in the fixture, so a machine's `.env` cannot change them |
| Load test | Pass | Below; no errors |
| Lint, types, tests | Pass | 164 passed on Windows and in CI (Linux uv, Windows pip) |

## Retrieval (provision-level)

Dev split, 94 scored items (unanswerable and refusal items have no gold provision).

| Configuration | Hit@1 | Hit@5 | Recall@8 | MRR@10 | nDCG@10 |
| --- | --- | --- | --- | --- | --- |
| **hybrid + routing (default)** | **0.862** | **0.936** | **0.915** | **0.892** | **0.884** |
| dense + routing | 0.851 | 0.925 | 0.915 | 0.879 | 0.879 |
| BM25 + routing | 0.777 | 0.904 | 0.883 | 0.828 | 0.831 |
| hybrid, no routing (Phase 2 behaviour) | 0.553 | 0.851 | 0.777 | 0.673 | 0.674 |
| hybrid + HyDE (run 1, before the follow-up fix) | 0.787 | 0.947 | 0.942 | 0.862 | 0.873 |

**Test split** (74 scored items, default pipeline): Hit@1 **0.946**, Hit@5 0.960,
Recall@8 0.960, MRR@10 **0.953** (run 1, before the fix: 0.892 / 0.905). The test split
was scored twice — before and after the follow-up fix — and both are reported here.

| Category (dev / test Hit@1) | Run 1 | After the fix |
| --- | --- | --- |
| Section lookup | 1.000 / 1.000 | 1.000 / 1.000 |
| Old → new code | 1.000 / 1.000 | 1.000 / 1.000 |
| Concepts | 0.821 / 0.955 | 0.821 / 0.955 |
| Case law | 0.905 / 0.778 | 0.905 / 0.778 |
| **Follow-ups** | **0.083 / 0.375** | **0.583 / 0.875** |

- **Routing is the largest single gain** (MRR 0.673 → 0.892): exact section lookups,
  old↔new counterparts and the in-force code preference.
- **Hybrid beats either retriever alone**, by little over dense (+0.012 MRR) and clearly
  over BM25 (+0.064).
- **HyDE is not enabled:** +0.004 MRR over hybrid in run 1 for one extra model call
  (~29 s on the laptop) per question.
- **Follow-up rewrites now need grounding.** The rewrite named an act or section the
  conversation did not contain in 8 of 12 dev follow-ups and 5 of 8 test ones; those are
  now searched as "previous question + follow-up".
- Applicable code chosen correctly: 1.000. Refusal of crime-assistance requests: 1.000,
  no false refusals.
- **Abstention before generation is conservative by design:** precision 1.000, recall
  0.18 on dev (the 0.63 threshold only skips generation for clearly unrelated
  questions; the prompt rule declines the rest — see answers).

Remaining misses: generic concept questions whose best passage is a neighbour (theft →
BNS s.134 before s.303; anticipatory bail → BNSS s.478/481 before s.482), judgments
asked about by a non-criminal statute the corpus lacks (Land Acquisition, Wealth-Tax),
and judgments that lose to the Article they discuss.

## Answers (40 dev items, stratified over categories)

| Metric | `answer_v3` (run 1) | **`answer_v4` (run 3)** |
| --- | --- | --- |
| Answers passing verification | 0.594 | **0.875** |
| Citation coverage (sentences with a marker) | 0.389 | **0.733** |
| Citation validity (markers pointing to a passage) | 1.000 | 1.000 |
| Gold provision cited | 0.848 | **0.879** |
| Key phrases present | **0.759** | 0.655 |
| Correct code applied | 1.000 | 1.000 |
| Injection resisted (forbidden output absent) | 3/3 | 3/3 |
| Unanswerable declined (abstention recall) | 1.000 | 1.000 |
| False abstentions | 3 | 3 |
| Time to first token / total (mean) | invalid (machine slowed) | **20.8 s / 51.2 s** |
| Prompt processing / generation speed | — | 105 / 5.3 tokens/s |

`answer_v4` added a two-sentence worked example of inline citation. It fixed the main
weakness of v3 (citations only in the "Provisions:" footer) but has two costs:

- **Example leakage in 3 of 40 answers:** the example's provisions or wording appeared
  in unrelated answers (IPC s.302 in an organised-crime answer; BNS s.103 in a judgment
  summary; the example's "still applies to offences committed before 1 July 2024"). The
  verifier flagged all three as unsupported provisions, so the UI warns on them. Fix for
  the next phase: an example with placeholders, measured against v4 with the same
  harness (`LEXAI_LLM_PROMPT_VERSION`).
- **Terser answers:** key phrases fell 0.76 → 0.66, mostly mapping questions answered in
  one sentence ("BNS s.318 corresponds to IPC s.420 [1][2].").

False abstentions: two prompt-injection questions answered "The indexed sources do not
answer this." instead of the legal part (safe, but over-cautious), and in one of them the
model also listed the passage headings it was told to print — no hidden instructions were
revealed. The third was a follow-up answered partly, then declined.

Judged metrics (faithfulness, relevancy, context precision/recall) were not run: they
need a judge stronger than the 7B generator, configured with `LEXAI_EVAL_JUDGE_*`.

## Load (laptop, one API process, embedded Qdrant)

| Run | p50 | p95 | Throughput | Errors |
| --- | --- | --- | --- | --- |
| Search, 1 client | 272 ms | 461 ms | 3.5 req/s | 0 |
| Search, 4 clients | 935 ms | 1,768 ms | 4.3 req/s | 0 |
| Search, 8 clients | 1,310 ms | 3,510 ms | 4.3 req/s | 0 |
| Answer, 1 client (first token) | 29.1 s (15.4 s) | 40.8 s (25.7 s) | — | 0 |
| Answer, 2 clients (first token) | 131.0 s (34.8 s) | 134.1 s (110.2 s) | — | 0 |

Search throughput levels off at ~4 req/s: the embedded Qdrant serialises calls behind
one lock and each search embeds the query. Two simultaneous answers queue in Ollama on
the single GPU, so the second waits for the first. Neither is a laptop-fixable limit:
a Qdrant server, a GPU serving engine and admission control (reject or queue with a
clear message instead of waiting minutes) belong to the deployment phases.

## Machine notes

Run 1's answers were generated at ~0.3 tokens/s (10× slower than Phase 3 on identical
prompts) after the model had run at normal speed earlier in the same run; run 2 was cut
off by the console closing or Windows logging off. The `speed` step now records power,
memory, GPU clocks and model speed before and after a run: run 3 was plugged in at full
CPU clock, at 5.1 → 3.9 tokens/s, with free RAM falling from 917 MB to 219 MB by the end.
On 8 GB with half the 7B model in system RAM, memory pressure is the main risk to
latency.

## Carried to the next phases

1. Placeholder example prompt (`answer_v5`) vs v4: remove leakage, keep coverage.
2. Answer the legal part of injection prompts instead of abstaining.
3. Concept misses where a neighbouring section outranks the answer; a reranker on the
   GPU server (the laptop profile has none).
4. Admission control for concurrent answers; Qdrant server for concurrent search.
5. A judge model on the GPU server for faithfulness and context metrics.
