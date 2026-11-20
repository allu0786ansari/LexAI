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
