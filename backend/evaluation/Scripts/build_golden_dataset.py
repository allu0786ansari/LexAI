"""
Assemble the 300-pair golden evaluation dataset from three sources, per
proposal §6.1:
  - 100 pairs from the Kaggle "LLM Fine-Tuning Dataset of Indian Legal
    Texts" (Akshat Gupta) — IPC/CrPC/Constitution QA.
  - 100 pairs sampled from IndicLegalQA V2 (NIT Srinagar), 20 each across
    factual / interpretive / procedural / contextual / predictive
    categories.
  - 100 synthetic pairs generated from the actual Data/ PDF corpus via
    RAGAS's TestsetGenerator.

Download the first two datasets yourself first (proposal §11 has exact
URLs — both require a free Kaggle/Mendeley account, neither could be
fetched automatically while building this). This script does NOT invent
QA pairs from a dataset it can't see — every non-synthetic pair here is
read from files you point it at.

Usage:
    python -m evaluation.build_golden_dataset \\
        --kaggle-csv /path/to/kaggle_indian_legal_qa.csv \\
        --indiclegalqa-json /path/to/indiclegalqa_v2.json \\
        --synthetic-count 100 \\
        --output evaluation/golden_dataset.json

Any source can be omitted (e.g. while you're still waiting on a Kaggle
download) — the script assembles whatever it's given and logs exactly how
many pairs came from where, rather than silently padding the shortfall.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

from app.config.logging import configure_logging, get_logger
from app.config.settings import get_settings

logger = get_logger(__name__)

IPC_CRPC_TARGET = 100
INDICLEGALQA_TARGET = 100
INDICLEGALQA_CATEGORIES = ["factual", "interpretive", "procedural", "contextual", "predictive"]
INDICLEGALQA_PER_CATEGORY = INDICLEGALQA_TARGET // len(INDICLEGALQA_CATEGORIES)
SYNTHETIC_DEFAULT = 100

# Column name candidates we'll try, in order, for the Kaggle CSV — public
# instruction-tuning datasets on Kaggle are inconsistent about naming.
# Override with --kaggle-question-col / --kaggle-answer-col if neither matches.
_KAGGLE_QUESTION_CANDIDATES = ["question", "instruction", "prompt", "query"]
_KAGGLE_ANSWER_CANDIDATES = ["answer", "output", "response", "completion"]


def _pick_column(fieldnames: list[str], candidates: list[str], override: str | None) -> str:
    if override:
        if override not in fieldnames:
            raise ValueError(f"Column {override!r} not found. Available columns: {fieldnames}")
        return override
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    raise ValueError(
        f"None of {candidates} found in CSV columns {fieldnames}. "
        f"Pass --kaggle-question-col/--kaggle-answer-col explicitly."
    )


def load_kaggle_ipc_crpc(
    csv_path: Path,
    target: int,
    question_col: str | None,
    answer_col: str | None,
    seed: int,
) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        logger.warning("kaggle_csv_empty", path=str(csv_path))
        return []

    q_col = _pick_column(reader.fieldnames or [], _KAGGLE_QUESTION_CANDIDATES, question_col)
    a_col = _pick_column(reader.fieldnames or [], _KAGGLE_ANSWER_CANDIDATES, answer_col)

    random.Random(seed).shuffle(rows)
    sampled = rows[:target]

    pairs = [
        {
            "question": row[q_col].strip(),
            "reference_answer": row[a_col].strip(),
            "category": "statute_qa",
            "source": "kaggle_akshatgupta7_indian_legal_qa",
        }
        for row in sampled
        if row.get(q_col, "").strip() and row.get(a_col, "").strip()
    ]
    logger.info("kaggle_pairs_loaded", requested=target, loaded=len(pairs), available=len(rows))
    return pairs


def load_indiclegalqa(json_path: Path, per_category: int, seed: int) -> list[dict]:
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)
    # IndicLegalQA V2 is typically a flat list of records. Be lenient about
    # a top-level {"data": [...]}-style wrapper too.
    records = raw if isinstance(raw, list) else raw.get("data", raw.get("records", []))
    if not records:
        logger.warning("indiclegalqa_empty_or_unrecognised_format", path=str(json_path))
        return []

    by_category: dict[str, list[dict]] = {c: [] for c in INDICLEGALQA_CATEGORIES}
    uncategorised: list[dict] = []
    for record in records:
        category = str(record.get("category", "")).strip().lower()
        if category in by_category:
            by_category[category].append(record)
        else:
            uncategorised.append(record)

    rng = random.Random(seed)
    pairs: list[dict] = []
    for category in INDICLEGALQA_CATEGORIES:
        pool = by_category[category] or uncategorised  # degrade gracefully if categories are missing
        rng.shuffle(pool)
        for record in pool[:per_category]:
            question = str(record.get("question", "")).strip()
            answer = str(record.get("answer", "")).strip()
            if not question or not answer:
                continue
            pairs.append(
                {
                    "question": question,
                    "reference_answer": answer,
                    "category": category if by_category[category] else "uncategorised",
                    "source": "indiclegalqa_v2",
                }
            )
    logger.info("indiclegalqa_pairs_loaded", loaded=len(pairs), target=INDICLEGALQA_TARGET)
    return pairs


def generate_synthetic_pairs(data_dir: Path, count: int) -> list[dict]:
    """
    Generate `count` synthetic QA pairs from the actual PDF corpus using
    RAGAS's TestsetGenerator (proposal §6.1: ~$3-5 in Gemini API calls for
    100 pairs). These are NOT manually reviewed by this script — the
    proposal calls for a 20-question manual spot check after generation;
    do that on the output file before trusting it as ground truth.
    """
    # Generate synthetic QA pairs using the local Ollama LLM provider.
    # This avoids any Google/Gemini usage or quota issues. The generator is
    # intentionally simple: for each sample we ask the model to produce a
    # short question-answer pair grounded in a passage.

    from ingestion.ingest import load_pdf_pages  # our own loader
    from app.services.providers import build_llm_client

    settings = get_settings()
    pdf_paths = sorted(p for p in data_dir.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
    documents = [page for pdf_path in pdf_paths for page in load_pdf_pages(pdf_path)]
    if not documents:
        raise RuntimeError(f"No PDFs found in {data_dir} to generate synthetic questions from.")

    llm = build_llm_client(settings, temperature=0, max_tokens=200)

    import random

    def _generate_from_passage(passage: str) -> tuple[str, str]:
        prompt = (
            "Read the following passage and generate a concise QA pair that can be answered using only the passage.\n\n"
            f"Passage:\n{passage}\n\n" "Output format:\nQuestion: <one-sentence question>\nAnswer: <short answer>"
        )

        chunks = []
        try:
            for chunk in llm.astream([{"type": "human", "content": prompt}]):
                chunks.append(getattr(chunk, "content", ""))
        except Exception:
            return ("", "")
        text = "".join(chunks).strip()
        # Try to parse "Question:"/"Answer:" pattern, fallback to naive split.
        q, a = "", ""
        if "Question:" in text and "Answer:" in text:
            try:
                q = text.split("Question:", 1)[1].split("Answer:", 1)[0].strip()
                a = text.split("Answer:", 1)[1].strip()
            except Exception:
                q = text
                a = ""
        else:
            parts = text.split("\n\n")
            if len(parts) >= 2:
                q = parts[0].strip()
                a = parts[1].strip()
            else:
                # As a last resort, make the passage the answer and synthesize a question
                a = text or passage[:200]
                q = (a.split(".", 1)[0] + "?") if a else "What is this passage about?"
        return q, a

    rng = random.Random(42)
    pairs = []
    for _ in range(count):
        passage = rng.choice(documents)
        q, a = _generate_from_passage(passage)
        if not q or not a:
            continue
        pairs.append({"question": q, "reference_answer": a, "category": "synthetic", "source": "local_ollama_synthetic"})

    logger.info("synthetic_pairs_generated", requested=count, generated=len(pairs))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kaggle-csv", type=Path, default=None)
    parser.add_argument("--kaggle-question-col", type=str, default=None)
    parser.add_argument("--kaggle-answer-col", type=str, default=None)
    parser.add_argument("--indiclegalqa-json", type=Path, default=None)
    parser.add_argument("--synthetic-count", type=int, default=0, help="0 skips synthetic generation (costs API credits).")
    parser.add_argument("--data-dir", type=Path, default=None, help="Defaults to Settings.data_dir.")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "golden_dataset.json")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    all_pairs: list[dict] = []

    if args.kaggle_csv:
        all_pairs.extend(
            load_kaggle_ipc_crpc(
                args.kaggle_csv, IPC_CRPC_TARGET, args.kaggle_question_col, args.kaggle_answer_col, args.seed
            )
        )
    else:
        logger.warning("skipping_kaggle_source", reason="--kaggle-csv not provided")

    if args.indiclegalqa_json:
        all_pairs.extend(load_indiclegalqa(args.indiclegalqa_json, INDICLEGALQA_PER_CATEGORY, args.seed))
    else:
        logger.warning("skipping_indiclegalqa_source", reason="--indiclegalqa-json not provided")

    if args.synthetic_count > 0:
        data_dir = args.data_dir or settings.data_dir
        all_pairs.extend(generate_synthetic_pairs(data_dir, args.synthetic_count))
    else:
        logger.info("skipping_synthetic_source", reason="--synthetic-count is 0")

    if not all_pairs:
        logger.error("no_pairs_assembled")
        sys.exit(1)

    random.Random(args.seed).shuffle(all_pairs)
    for i, pair in enumerate(all_pairs):
        pair["id"] = f"gq_{i:04d}"

    split_index = int(len(all_pairs) * 0.8)
    dev_set = all_pairs[:split_index]
    test_set = all_pairs[split_index:]

    output = {
        "total": len(all_pairs),
        "dev_set_size": len(dev_set),
        "test_set_size": len(test_set),
        "source_counts": {
            source: sum(1 for p in all_pairs if p["source"] == source)
            for source in {p["source"] for p in all_pairs}
        },
        "dev_set": dev_set,
        "test_set": test_set,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(
        "golden_dataset_written",
        path=str(args.output),
        total=len(all_pairs),
        dev=len(dev_set),
        test=len(test_set),
        source_counts=output["source_counts"],
    )
    if len(all_pairs) < 300:
        logger.warning(
            "golden_dataset_below_target",
            target=300,
            actual=len(all_pairs),
            hint="Provide all three sources (--kaggle-csv, --indiclegalqa-json, --synthetic-count 100) to reach the proposal's 300-pair target.",
        )


if __name__ == "__main__":
    main()
