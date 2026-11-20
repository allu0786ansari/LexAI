# Phase 3 — verified results

Run 3 on the target laptop, 20 Nov 2026, commit `d0d9121`, logs in [`output/`](output/).
Reviewed against the Phase 3 exit criteria in the implementation plan. Runs 1–2
and what each fixed are in the [run history](README.md#run-history).

**Machine:** Windows (build 26300), 8 GB RAM, NVIDIA GTX 1650 4 GB, Python 3.12.4, Ollama 0.40.9.
**Profile:** `laptop-8gb v3` — Qwen2.5-7B-Instruct Q4_K_M (4K context), nomic-embed-text,
no reranker, 6,000-character passage budget, abstention threshold 0.63. Index from Phase 2
(63 documents, 5,070 chunks).

## Exit criteria

| Criterion | Result | Evidence (`06_phase3.log`) |
| --- | --- | --- |
| Section lookups return the exact section as passage [1] | Pass | 4/4: BNS s.103, CrPC s.438 (+ BNSS s.482), Constitution Art.21, IEA s.65B (+ BSA s.63); the full answer cites BNS s.103 as [1] |
| Follow-ups retrieve as well as the standalone question | Pass | "Is it bailable?" → "Is the punishment for murder under the BNS bailable?" (finds BNS s.103); "What was the old IPC section for it?" → IPC s.302 first |
| Every streamed answer ends with a verification event | Pass | `meta, citations … verification, done`; valid, coverage 1.0 |
| Unanswerable questions are refused rather than answered from memory | Pass | GST question declined before generation (0.614 < 0.63); borderline ones answered "The indexed sources do not answer this." by the prompt rule; 0 of 10 answerable questions declined |
| Old → new correspondence | Pass | 44/45 known pairs: IPC→BNS 20/21, CrPC→BNSS 12/12, IEA→BSA 12/12 |
| Code in force first | Pass | "Punishment for murder?" → BNS s.103, IPC s.302, BNS s.104 … |
| Mapping questions | Pass | IPC s.420 → BNS s.318 (similarity 0.78); CrPC s.154 → BNSS s.173 |
| Guardrails, cache | Pass | Evidence-destruction request refused without a model call; repeat question from cache in 0.01 s |
| Lint, types, tests; Phase 2 smoke | Pass | 140 passed on Windows (`03_quality.log`); `07_smoke.log` ALL PASSED |

## Measured on this laptop

| Measure | Value |
| --- | --- |
| Prompt size (section lookup) | 1,412 tokens (was ~2,000 in Phase 1–2) |
| Time to first token | 11 s (was 32 s in Phase 2) |
| Total answer time (~70 tokens) | 25 s |
| Retrieval (embed + hybrid + exact lookups + counterparts) | 300–440 ms |
| Correspondence built at start-up | 2,262 sections in 1.5 s |
| Declined before generation | 0.4 s |
| Cache hit | 0.01 s |
| Dense similarity, answerable / unanswerable | 0.687–0.849 / 0.614–0.681 |

## Findings carried into Phase 4

1. **Qwen 7B sometimes omits citations.** One of four generated answers (a
   correct one on BNS s.103) had no `[n]` markers; the verifier flagged it, it
   was not cached, and the UI would show a warning. Phase 4 measures the rate
   on the golden set; if it is material, try a stricter prompt or one
   regeneration on a `no_citations` flag.
2. **Bailability lives in a table.** "Is murder bailable?" finds BNS s.103, but
   the answer is in the BNSS First Schedule (classification of offences), which
   the follow-up did not retrieve. Phase 4 should test schedule questions and
   consider indexing schedule rows as their own passages.
3. **The abstention margin is thin.** Answerable and unanswerable scores are
   separated by 0.006 on 16 questions; the laptop uses 0.63, well below the
   boundary. Phase 4 calibrates on the golden set's unanswerable items.
4. **One correspondence miss.** IPC s.379 → BNS s.134 instead of s.303: IPC 379
   is one sentence inside the much longer BNS 303(2). Correspondence accuracy
   is measured on a full table in Phase 4.
5. **Section numbers with no act** ("section 302" with no code named) are looked
   up in the codes that apply; questions that name only the old code switch the
   applicable law to it, which is right for "under the IPC" but needs golden
   items to confirm the wording of answers.
