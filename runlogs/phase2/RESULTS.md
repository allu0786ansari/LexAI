# Phase 2 — verified results

Run 4 on the target laptop, 20 Nov 2026, commit `52a4c86`, logs in [`output/`](output/).
Reviewed against the Phase 2 exit criteria in the implementation plan. Runs 1–3 and
what each fixed are in the [run history](README.md#run-history).

**Machine:** Windows (build 26300), 8 GB RAM, NVIDIA GTX 1650 4 GB, Python 3.12.4, Ollama 0.40.9.
**Profile:** `laptop-8gb v2` — Qwen2.5-7B-Instruct Q4_K_M (4K context), nomic-embed-text,
no reranker, 1,400-character chunks, 6,000-character passage budget, embedded Qdrant.

## Exit criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Lint, format, types and tests clean | Pass | ruff, mypy clean; 72 passed on Windows, incl. the 3 checks on the downloaded Gazette prints (`03_quality.log`) |
| Corpus is reproducible and pinned | Pass | 5 downloads pinned by SHA-256 in `data/manifest.lock.json`; 58 bundled files match `SHA256SUMS` (`05_fetch.log`) |
| BNS, BNSS, BSA indexed alongside IPC, CrPC, IEA | Pass | all 63 documents live (`08_status.log`) |
| Statutes chunked by section/article with titles | Pass | BNS 358, BNSS 531, BSA 170, IPC 548, CrPC 532, IEA 179, Constitution 486 articles, IT Act 110, CLA 2018 26 — every section titled (`06_parse.log`) |
| Build checks gate publication | Pass | 10 content checks + chunk count pass before the alias moves (`07_build.log`); runs 1–2 showed an incomplete corpus is refused and the live index left unchanged |
| Exact provision lookup | Pass | 8/8: BNS s.103, IPC s.302, BNSS s.173, CrPC s.154, BSA s.63, IEA s.65B, Art. 21, IT Act s.66 (`09_inspect.log`) |
| Hybrid search finds the right provision | Pass (smoke level) | 6/6 questions hit in the top 10 (`09_inspect.log`); full evaluation is Phase 4 |
| Citations name the provision and its status | Pass | "IPC s.302 — Punishment for murder … repealed 2024-07-01; replaced by BNS; applies to earlier offences" (`10_smoke.log`) |
| API still non-blocking with Qdrant | Pass | `/healthz` p50 3.8 ms, p95 8.6 ms over 335 probes during 2 parallel answers |

## Measured on this laptop

| Measure | Value |
| --- | --- |
| Corpus | 63 documents, 5,070 chunks |
| Full build (parse + embed with nomic-embed-text + checks) | 594 s |
| Parse only (`--dry-run`) | ~45 s |
| Retrieval latency (dense + BM25 + RRF in Qdrant) | 310 ms |
| Time to first token, RAG prompt | 32 s |
| Total answer time (45 tokens) | 39 s |

## Findings carried into Phase 3

1. **Which code applies.** "What is the punishment for murder?" (no code named)
   ranks IPC s.302 first and BNS s.103 second. Both are right for different
   offence dates; Phase 3's query handling should prefer the code in force and
   mention the old one, or ask for the date of the offence.
2. **Vocabulary gaps.** "Anticipatory bail" finds CrPC s.438 only at rank 10 and
   BNSS s.482 not at all: neither section uses the phrase. Phase 3 adds the
   reranker (server profile) and legal-term query expansion; Phase 4 measures it.
3. **Noise in two sources.** The IIT Kanpur IPC loses spaces in some headings
   ("Of offencesaffectingthe human body"); the Prisons Act compilation has no
   parseable section structure and is indexed as plain text.
4. **India Code was unreachable** from this network for the whole session (504
   on every PDF). The PRS Gazette prints are now pinned; India Code stays first
   in the manifest for a fresh machine.
5. **Time to first token (~32 s)** is prompt processing with half of Qwen on the
   CPU. Phase 3 trims the prompt (fewer, better passages after reranking).
