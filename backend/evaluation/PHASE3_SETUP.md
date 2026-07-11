# Phase 3 — Evaluation Suite: Setup & Run

## What this contains

New `evaluation/` package (top-level, alongside `ingestion/` — matches proposal §9):
- `_ragas_compat.py` — fixes a real, verified upstream bug in ragas 0.4.3 (see below). Import this before importing anything from `ragas`.
- `build_golden_dataset.py` — assembles the 300-pair golden set from 3 sources (proposal §6.1)
- `run_ragas.py` — end-to-end RAGAS evaluation against the live QA service (proposal §7.1)
- `run_benchmark.py` — LegalBench-RAG-mini retrieval ablation study (proposal §7.2/§7.3)
- `golden_dataset.json` — **a 10-pair STARTER set only**, not the real 300 (see below)
- `results/` — where JSON output lands
- `requirements.txt` — adds `ragas` on top of the main backend requirements

Nothing in `backend/app/` or `backend/ingestion/` changes in this phase.

## A real bug I hit building this, and how it's fixed

`pip install ragas` today pulls in `ragas==0.4.3`, which unconditionally does `from langchain_community.chat_models.vertexai import ChatVertexAI` at import time — even though this project never touches Vertex AI (it uses Gemini via `langchain-google-genai`). That submodule doesn't exist in current `langchain-community`, so **`import ragas` fails outright** on a clean install, before you even pick an LLM provider.

I verified this isn't a version-pinning problem on our end: pinning `langchain-community` down far enough to have that submodule cascades into a `langchain-core` conflict with `langchain-google-genai` (tried it — real conflict, not hypothetical). The actual fix is a tiny, honest module stub in `_ragas_compat.py` that short-circuits the dead import path. It's verified to coexist fine with this project's real `langchain-core==1.4.9` / `langchain-google-genai==4.2.7` — no downgrades needed anywhere. `evaluation/requirements.txt` being separate from the main one is just to keep `ragas`'s extra dependencies (`datasets`, `instructor`, `openai`, `tiktoken`) out of the serving container, not because of an unresolvable conflict.

**Every script here imports the stub automatically** (`import evaluation._ragas_compat` at the top) — you don't need to do anything extra, just don't remove that line if you ever edit these files.

## Setup

```bash
cd backend
pip install -r evaluation/requirements.txt
```

## Step 1 — Build the real golden dataset (optional but recommended)

The shipped `evaluation/golden_dataset.json` is a **10-question starter set I wrote by hand** from your actual `Data/` PDFs, so `run_ragas.py` has something to run immediately. It is explicitly not the proposal's 300-pair target — I don't have Kaggle or Mendeley account access from the build environment, so I couldn't download the real source datasets.

To build the real one:
```bash
# 1. Download from proposal §11:
#    - Kaggle: "LLM Fine-Tuning Dataset of Indian Legal Texts" (Akshat Gupta)
#    - Mendeley: IndicLegalQA V2 (NIT Srinagar)

python -m evaluation.build_golden_dataset \
  --kaggle-csv /path/to/kaggle_dataset.csv \
  --indiclegalqa-json /path/to/indiclegalqa_v2.json \
  --synthetic-count 100 \
  --output evaluation/golden_dataset.json
```
Any source can be omitted if you're not ready with it yet — the script tells you exactly how many pairs it got from where, and warns if the total is under 300 rather than pretending it hit the target. `--synthetic-count 100` costs real Gemini API credits (proposal estimates $3-5) and takes several minutes.

The script auto-detects common Kaggle column-naming conventions (`question`/`answer`, `instruction`/`output`, etc.) — verified against both patterns. If yours uses something else, pass `--kaggle-question-col` / `--kaggle-answer-col` explicitly.

**Do the proposal's 20-question manual spot-check on the synthetic pairs** before trusting them as ground truth — this script doesn't do that review for you.

## Step 2 — Run the RAGAS evaluation

Requires your real `GOOGLE_API_KEY` and a built `Database/` (from Phase 1) — this calls your live `LegalQAService` in-process for every question.

```bash
python -m evaluation.run_ragas                    # full test_set
python -m evaluation.run_ragas --split dev         # dev_set instead
python -m evaluation.run_ragas --subset 30 --gate  # CI-gate use case (proposal §7.5): 30-question subset, exit 1 on failure
```

Output: a results table in the terminal + `evaluation/results/ragas_<split>_<timestamp>.json`.

## Step 3 — Run the retrieval ablation benchmark

Requires LegalBench-RAG-mini data, which — like the golden-set sources — I couldn't download automatically (it's distributed via Dropbox / a Hugging Face mirror, neither reachable from the build sandbox). Get it from the link in `github.com/zeroentropy-cc/legalbenchrag`'s README (proposal §11 also references this).

```bash
# Expected layout once downloaded:
#   data/benchmarks/*.json
#   data/corpus/*

python -m evaluation.run_benchmark --benchmark-dir data --benchmarks privacy_qa contractnli
```

Output: the 3-row ablation table (baseline / hybrid / full) + `evaluation/results/benchmark_<timestamp>.json`.

**Verified against the real upstream repo, not guessed**: I cloned `zeroentropy-cc/legalbenchrag` to read its actual `benchmark_types.py` and `run_benchmark.py` source before writing this — the JSON schema (`{"tests": [{"query", "snippets": [{"file_path", "span"}]}]}`) and the character-overlap precision/recall formula are copied exactly from their code, so a real download drops in without reshaping. nDCG@10 is this project's own addition on top (upstream only computes precision/recall) — implemented with graded relevance (per-chunk overlap ratio), not just binary hit/miss.

## What was tested before delivery (and what wasn't)

Tested for real, in a sandbox:
- The classic `ragas.evaluate()` pipeline end-to-end (fake LLM/embeddings, real ragas internals) — confirmed it runs the full harness: prompting, output parsing, retries, aggregation, without crashing on wiring
- `run_ragas.py`'s own glue code (`_collect_samples`) against a real mocked `LegalQAService` — confirmed it correctly captures the real streamed answer + real retrieved context per question
- `build_golden_dataset.py` end-to-end against synthetic CSV/JSON fixtures matching two different real-world Kaggle column-naming conventions — exact sample counts, exact 20-per-category balance, correct 80/20 split
- Precision/recall/nDCG formulas against hand-computed known cases (perfect match, partial overlap, wrong file, ranking order) — all matched expected values exactly
- `run_benchmark.py`'s full pipeline (baseline/hybrid/full configs) against a synthetic fixture built in the exact verified upstream schema

Not tested (can't be, without your API key / the real datasets): a real Gemini-judged RAGAS score, or real benchmark numbers on actual LegalBench-RAG-mini data. The ablation table's three rows will very likely look different from each other once you run them for real, unlike the identical-looking numbers you'd get from my tiny 2-chunk test fixture.

## Before Phase 4

Phase 4+ (containerization, CI/CD, deployment) can wire `run_ragas.py --subset N --gate` into a GitHub Actions step per proposal §7.5 — no changes needed here first.
