# ADR 0003 — Phase 3 query understanding, routing and grounded generation

Date: 2026-11-20 · Status: proposed (pending verification on the target laptop, see runlogs/phase3/)

## Context

After Phase 2 the index was correct and complete, but every question went
through the same hybrid search. A question naming "Section 103 BNS" could rank
the section anywhere; "punishment for murder" ranked the repealed IPC above the
BNS; follow-ups ("and is it bailable?") were searched literally; nothing checked
that the answer's citations and section numbers came from the passages; and a
question outside the corpus was answered from whatever ranked first.

## Decisions

1. **Rules before models for understanding the question.** References ("302
   IPC", "Art. 19(1)(a)", "sections 154 and 156 CrPC"), intent (section lookup,
   old→new mapping, case law, concept, refused, out of scope) and the applicable
   code are decided by regexes and an alias table (`lexai/query/`). They are
   instant, testable (one parametrised test per form) and shown to the client in
   the `meta` event. The plan's model fallback for unclear intents is deferred:
   on the 8 GB laptop each extra call costs 5–10 s, and Phase 4's evaluation
   will show whether the rules miss enough to justify it.
2. **One model call for follow-ups, only when needed.** From the second turn,
   a message that leans on the conversation (short, pronouns, "and…", no act of
   its own) is rewritten into a standalone question at temperature 0. The
   rewrite is used for search and generation; the user's words are kept in the
   session. A failed rewrite falls back to the original message.
3. **Exact provisions are pinned.** Named sections are fetched by payload filter
   and placed first, whole (pieces re-joined without the splitter's overlap). A
   section without its act is looked up in the codes that apply.
4. **The old↔new correspondence is derived, not typed.** Each IPC/CrPC/IEA
   section is matched to the BNS/BNSS/BSA section with the most similar title
   and text (TF-IDF cosine, title weighted ×3; threshold 0.35; runners-up within
   90% kept), computed from the live index at start-up (~1 s). Each section is
   also compared with every numbered sub-section of the other code's sections
   and the better score counts, because the BNS often merged a definition and
   its punishment (IPC 378 + 379 → BNS 303, with the punishment in 303(2)). A hand-typed
   table of ~1,000 rows would be a source of silent legal errors; the derived
   one is reproducible, carries its score, and is presented as "corresponds
   to". It reproduces 20/20 well-known CrPC→BNSS pairs; the laptop run checks
   45 pairs across all three codes (run 1: 42/45 before sub-section matching).
5. **Which code applies.** An explicit event date before 1 July 2024, "before
   July 2024", or naming only an old code selects IPC/CrPC/IEA; otherwise the
   BNS/BNSS/BSA. The applicable code's counterparts (best match and a close
   runner-up, e.g. IPC 302 → BNS 103 and 104) are inserted (or moved) just
   before a section of the other code in the results, and the prompt states the
   rule and the correspondences.
6. **Abstention on a calibrated score, never on a guess.** With a reranker the
   best rerank score is used; without one (the laptop) the best dense cosine
   similarity. The threshold is unset until measured. On the laptop
   (nomic-embed-text) answerable questions scored 0.687–0.849 and unanswerable
   ones 0.614–0.681; the midpoint would sit 0.003 from a real question, so the
   laptop profile uses 0.63: it skips generation only for clearly unrelated
   questions and leaves borderline ones to the prompt rule. Questions that name a section are never abstained on. The prompt
   also requires "The indexed sources do not answer this." when they do not.
7. **Citations are verified after the stream.** Markers pointing to no passage
   are removed (the `done` event carries the cleaned answer); every section or
   article the answer mentions must be a passage's provision or appear in a
   passage's text, otherwise it is flagged; coverage is the share of claim
   sentences with a marker. The result is the `verification` event; flagged
   answers are logged and not cached.
8. **Context budget shared fairly.** Exact provisions and their counterparts
   are all kept, each cut to an equal share of the character budget if needed;
   searched passages fill the rest. A search hit inside a unit of more than four
   pieces (a schedule, a long judgment) shows only the retrieved pieces, never
   the whole unit. Without this a long CrPC section crowded out
   its BNSS counterpart on the laptop's 6,000-character budget.
9. **Prompt as a versioned file** (`generation/templates/answer_v3.md`):
   short answer → explanation → "Provisions:" line; passages inside a delimited
   block that passage text cannot close; the version is in every `done` event
   and log line.
10. **Guardrails without a model.** Requests for help committing or covering up
    a crime are refused before search; lawful defensive questions ("how can I
    avoid arrest in a false case" → anticipatory bail) are answered. Small talk
    and clearly non-legal topics get a short redirect. Personal data is masked
    in every log line.
11. **Cache.** Verified answers are cached by normalised standalone question,
    index version, prompt version and model (in-process LRU by default, Redis
    when configured); query embeddings are cached in the embedder.
12. **SSE contract v1:** `meta` → `citations` → `token`* → `verification` →
    `done`, or `error`. The v0.1 frontend ignores the new events; the current
    UI shows the verification result.

## Consequences

- The laptop pays ~5–10 s more for follow-ups; first questions are unchanged.
- `POST /api/search` returns what the model would see, for debugging and the
  Phase 4 evaluation.
- Phase 4 measures what this phase leaves as defaults: the abstention threshold,
  the reranker on the server profile, HyDE, and whether a model-based intent
  fallback is worth its latency.
