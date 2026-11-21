# Phase 4 — run and report (Windows Command Prompt)

Phase 4 measures LexAI instead of spot-checking it: a 200-question golden set,
retrieval and answer metrics, ablations that show what each part of the pipeline
is worth, a load test, and a regression gate that runs in GitHub Actions on every
pull request without any model server. Everything runs in **cmd.exe**. Run the
steps below, then push the results. Phase 5 starts only after they are reviewed.

## What changed for you

- **Golden set** `eval/datasets/golden_v1.jsonl`: 200 questions in 7 categories
  (section lookup 40, concepts 50, old→new code 25, case law 30, unanswerable 25,
  multi-turn 20, prompt-injection 10). Each has gold provisions (e.g. `BNS s.103`)
  or judgments, key phrases a good answer contains, and flags such as "should
  abstain". Before scoring, every item is checked against the indexed text; an
  item whose gold section is missing or whose key phrase is not in it is excluded
  and listed, never silently scored.
- **Dev / test split.** About 60% of items are *dev* (look at them, tune on them);
  the rest are *test*, scored once per release. After this run the test split is
  locked by checksum: editing a test item makes the harness refuse to run.
- **Metrics.** Retrieval: Hit@1, Hit@5, Recall@8, MRR@10, nDCG@10, by category;
  abstention precision/recall; refusal recall; correct code applied. Answers:
  citation validity, gold provision cited, key phrases present, verification,
  injection resistance, time to first token. Judged metrics (faithfulness,
  relevancy, context precision/recall) run only when a judge model larger than
  Qwen 7B is configured, so they are reported as "not run" on the laptop.
- **Ablations** on the dev split: hybrid (default), dense only, BM25 only, no
  routing (Phase 2 behaviour), HyDE.
- **CI.** `.github/workflows/ci.yml` runs lint, types and tests on Linux and on
  Windows with pip, builds the frontend, and runs the **retrieval gate**: the
  index exported by this run (`eval/fixtures`) is loaded into an in-memory Qdrant
  and the dev smoke set is searched with stored query vectors; the PR fails if
  Recall@8 or MRR@10 drops more than 0.02 below `eval/baselines/main.json`.
- **Load test** `scripts/load_test.py`: search latency at 1/4/8 concurrent users
  and answer latency at 1/2.

No re-download or rebuild: the Phase 2 index is reused. No new packages.

## 1. Get the branch

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git fetch origin
git checkout phase-4-evaluation
git pull
```

## 2. Run everything (about 45–60 minutes)

Close any LexAI API you started yourself, keep the laptop plugged in, then:

```bat
runlogs\phase4\run_phase4.bat
```

| Step | What it does | Log |
| --- | --- | --- |
| `check` | Git state, Python, Ollama, GPU, RAM, port 8000 | `01_check.log` |
| `setup` | pip install (no new packages) | `02_setup.log` |
| `quality` | ruff, format check, mypy, tests | `03_quality.log` |
| `models` | Models present and responding | `04_models.log` |
| `validate` | Every golden item checked against the index | `05_validate.log` |
| `retrieval` | Dev split, 5 configurations (~20 min; HyDE is most of it) | `06_retrieval_dev.log` |
| `test` | Held-out test split, default pipeline | `07_retrieval_test.log` |
| `answers` | 40 full answers across all categories (~20 min) | `08_answers.log` |
| `fixture` | Exports the CI fixture, writes the baseline, checks the gate passes and that turning routing off fails it | `09_fixture.log` |
| `load` | Starts the API; search and answer latency under concurrency (~5 min) | `10_load.log`, `10_server.log` |
| `report` | Writes `eval\reports\<commit>\report.md` | `11_report.log` |

A step can be re-run alone, e.g. `runlogs\phase4\run_phase4.bat answers report`.

## 3. Send the results

```bat
git add runlogs\phase4\output eval\reports eval\fixtures eval\baselines
git commit -m "Phase 4 run logs"
git push
```

The fixture is roughly 10–20 MB (the index's vectors at half precision and its
text); it is what lets CI test retrieval without Ollama.

## Run 2 (after the run 1 fixes)

Run 1 found three problems, fixed on this branch (see Run history). Run 2
re-measures what they affect. Before starting: **plug the laptop in, set
Windows to the "Best performance" power mode, and close other programs
(browser, OneDrive sync if possible)**. Run 1's answers were generated ~10×
slower than Phase 3 on the same questions; the new `speed` step records power,
memory, GPU clocks and the model's speed before and after, so a slow machine is
visible in the log instead of looking like a slow pipeline.

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git pull
runlogs\phase4\run_phase4.bat speed setup quality models retrieval-quick test answers fixture load speed report
```

About 50 minutes at normal speed (`retrieval-quick` skips HyDE: run 1 measured
it, +0.004 MRR for ~29 s per question). If the first `speed` block in
`00_speed.log` shows generation under 2 tokens/s, stop (Ctrl+C), restart the
laptop, and run the same command again.

Then send the results:

```bat
git add runlogs\phase4\output eval\reports eval\fixtures eval\baselines
git commit -m "Phase 4 run 2 logs"
git push
```

## Run 3 (finish run 2)

Run 2's retrieval steps completed; the run stopped during the answers step
(see Run history). Run 3 does only the remaining steps. The answers step now
saves each answer as it finishes, so if it is interrupted again, running the
same command continues where it stopped.

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git pull
runlogs\phase4\run_phase4.bat speed answers fixture load speed report
```

About 35 minutes. Keep the window open and the laptop plugged in and awake
until it prints "Done." Then:

```bat
git add runlogs\phase4\output eval\reports eval\fixtures eval\baselines
git commit -m "Phase 4 run 3 logs"
git push
```

## What "pass" looks like

| Log | Pass |
| --- | --- |
| `03_quality.log` | every command `exit=0` |
| `05_validate.log` | 200/200 items valid (an invalid item is a dataset bug I fix) |
| `06_retrieval_dev.log` | five result lines; `hybrid` at or above `no-routing` on Hit@1 and MRR@10 |
| `08_answers.log` | `errors: 0` |
| `09_fixture.log` | `GATE PASSED` for the baseline, then `GATE FAILED` for `no-routing` (planted regression) |
| `10_load.log` | `SUMMARY: NO ERRORS` |

The numbers themselves are the result: they go into `RESULTS.md` and the ADR,
including the weak spots.

## Run history

| Run | Date | Result |
| --- | --- | --- |
| 1 | 2026-11-20 | Quality 153 passed; **200/200 items valid**. Retrieval (dev, hybrid): Hit@1 0.798, Hit@5 0.936, Recall@8 0.915, MRR@10 0.858; routing is worth +0.21 MRR (no-routing 0.646); dense 0.841, BM25 0.803, HyDE 0.862 (+0.004 for ~29 s/question). Test split: Hit@1 0.892, MRR@10 0.905. Gate: baseline passes, routing-off fails; Linux reproduces the Windows numbers exactly. Answers (40): gold provision cited 0.85, key phrases 0.76, code applied 1.00, injection resisted 3/3, refusal recall 1.00. **Found:** (a) follow-ups: Hit@1 0.08 on dev — the 7B rewrite inserted old-code sections and even invented acts ("Is it bailable?" → "...Section 302 of the Indian Penal Code"; "Bureau of Investigation Act"), which the router pinned first; (b) 11 of 30 answers cited only in the "Provisions:" footer, never inline (coverage 0.39); correct abstentions were flagged "no citations"; (c) answers were generated at ~0.3 tokens/s, ~10× slower than Phase 3 on identical prompts (HyDE earlier in the same run was at normal speed): the laptop slowed down mid-run, so run 1 latency figures are invalid. **Fixed:** rewrites may only name acts the user named and sections in the conversation, else the previous question + follow-up are searched verbatim (replayed on run 1's 20 rewrites: the 14 hallucinated ones rejected, the 6 grounded kept); the user's own era words outrank the rewrite; prompt `answer_v4` with a worked citation example and an injection rule; abstentions not flagged; `speed` diagnostics; token rates in the report; test split locked. |
| 2 | 2026-11-21 | Quality 163 passed; model at 6.4 tokens/s (normal). Retrieval improved where the fix applied and nowhere else: follow-ups Hit@1 **0.083 → 0.583** (dev) and **0.375 → 0.875** (test); overall dev Hit@1 0.798 → **0.862**, MRR@10 0.858 → **0.892**; test Hit@1 0.892 → **0.946**, MRR@10 0.905 → **0.953**; every other category unchanged. Answers were back to normal speed (first token 10–28 s, 4–6 tokens/s, 15–58 s per answer; run 1's slowness was the machine). **Interrupted:** at 07:06, after 37 of 40 answers, the answers step and Ollama both ended with code 0x40010004 (the console was closed, or Windows logged off or restarted); nothing after it ran, and the answers were lost because they were saved only at the end. Also, `ollama ps` in the `speed` step had started the Ollama server inside the script's console (Ollama was not running), which held `00_speed.log` open (later writes to it were lost) and tied Ollama to that window. **Fixed:** answers are saved one by one and a rerun resumes; Ollama is started in its own window before any step that uses it. |

