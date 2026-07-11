"""
Runs the RAGAS end-to-end evaluation (proposal §7.1) against the live QA
service: for every golden-set question, gets a real answer + retrieved
contexts from LegalQAService, then scores Faithfulness, Context Precision,
Answer Relevancy, and Context Recall.

MUST be run in the isolated evaluation environment — see
evaluation/requirements.txt and PHASE3_SETUP.md for why (ragas 0.4.3
has a real upstream dependency conflict with this project's main
LangChain stack; see evaluation/_ragas_compat.py for details).

Usage:
    python -m evaluation.run_ragas                      # full test_set
    python -m evaluation.run_ragas --split dev           # dev_set instead
    python -m evaluation.run_ragas --subset 30           # first N only (CI gate use case, proposal §7.5)
    python -m evaluation.run_ragas --gate                # exit 1 if faithfulness<0.75 or context_precision<0.65
"""
from __future__ import annotations

import evaluation._ragas_compat  # noqa: F401  side-effect import, must precede ragas imports

import argparse
import asyncio
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from app.config.logging import configure_logging, get_logger
from app.config.settings import get_settings

logger = get_logger(__name__)

GATE_THRESHOLDS = {"faithfulness": 0.75, "context_precision": 0.65}
TARGET_SCORES = {"faithfulness": 0.80, "context_precision": 0.75, "answer_relevancy": 0.80, "context_recall": 0.70}


async def _collect_samples(golden_pairs: list[dict]):
    """
    Runs each golden question through the real QA service to get a real
    answer and real retrieved contexts. Uses LegalQAService directly
    (in-process) rather than hitting the HTTP API, so this can run without
    a separately-running server — but it means this script needs the same
    Database/ index and GOOGLE_API_KEY as the API does.
    """
    from ragas import SingleTurnSample

    from app.services.qa_service import get_qa_service

    qa_service = get_qa_service()
    samples = []
    for i, pair in enumerate(golden_pairs):
        session_id = f"ragas-eval-{pair.get('id', i)}"  # isolated session per question, no cross-contamination
        answer_chunks: list[str] = []
        contexts: list[str] = []
        async for event in qa_service.stream_answer(pair["question"], session_id):
            if event["event"] == "citations":
                # We only have citation metadata here, not full chunk text — re-fetch
                # the actual context text from the retriever trace instead, see below.
                pass
            elif event["event"] == "token":
                answer_chunks.append(event["data"]["text"])
            elif event["event"] == "error":
                logger.error("qa_service_error_during_eval", question=pair["question"], error=event["data"])

        # Re-run retrieval directly to get full context text for RAGAS (the SSE
        # 'citations' event only carries metadata, not the chunk text itself —
        # deliberately kept light on the wire; RAGAS needs the actual text).
        fused, _trace = qa_service.retriever.retrieve(pair["question"])
        if qa_service.reranker is not None:
            reranked = qa_service.reranker.rerank(pair["question"], [f.document for f in fused], top_n=qa_service.settings.rerank_top_n)
            contexts = [r.document.page_content for r in reranked]
        else:
            contexts = [f.document.page_content for f in fused[: qa_service.settings.rerank_top_n]]

        samples.append(
            SingleTurnSample(
                user_input=pair["question"],
                response="".join(answer_chunks),
                retrieved_contexts=contexts,
                reference=pair.get("reference_answer", ""),
            )
        )
        if (i + 1) % 10 == 0:
            logger.info("eval_progress", completed=i + 1, total=len(golden_pairs))
    return samples


def run_ragas_eval(golden_pairs: list[dict]) -> dict:
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness
    from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

    settings = get_settings()

    samples = asyncio.run(_collect_samples(golden_pairs))
    dataset = EvaluationDataset(samples=samples)

    judge_llm = LangchainLLMWrapper(
        ChatGoogleGenerativeAI(model=settings.llm_model, google_api_key=settings.require_google_api_key(), temperature=0)
    )
    judge_embeddings = LangchainEmbeddingsWrapper(
        GoogleGenerativeAIEmbeddings(model=settings.embedding_model, google_api_key=settings.require_google_api_key())
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ragas emits noisy deprecation warnings for the classic metrics API
        result = evaluate(
            dataset=dataset,
            metrics=[Faithfulness(), ContextPrecision(), AnswerRelevancy(), ContextRecall()],
            llm=judge_llm,
            embeddings=judge_embeddings,
            raise_exceptions=False,
        )

    metric_names = ["faithfulness", "context_precision", "answer_relevancy", "context_recall"]
    scores_df = result.to_pandas()
    scores = {name: float(scores_df[name].mean()) for name in metric_names if name in scores_df.columns}
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden-dataset", type=Path, default=Path(__file__).parent / "golden_dataset.json")
    parser.add_argument("--split", choices=["dev", "test"], default="test")
    parser.add_argument("--subset", type=int, default=None, help="Evaluate only the first N questions.")
    parser.add_argument("--gate", action="store_true", help="Exit 1 if scores fall below the CI gate thresholds (proposal §7.5).")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    with open(args.golden_dataset, encoding="utf-8") as f:
        golden = json.load(f)
    pairs = golden[f"{args.split}_set"]
    if args.subset:
        pairs = pairs[: args.subset]

    if not pairs:
        logger.error("no_golden_pairs_found", split=args.split, path=str(args.golden_dataset))
        sys.exit(1)

    logger.info("ragas_eval_starting", split=args.split, count=len(pairs))
    scores = run_ragas_eval(pairs)
    logger.info("ragas_eval_complete", scores=scores)

    print("\n=== RAGAS Evaluation Results ===")
    print(f"{'Metric':<20} {'Score':<10} {'Target':<10} {'Status'}")
    for metric, target in TARGET_SCORES.items():
        score = scores.get(metric, float("nan"))
        status = "PASS" if score >= target else "BELOW TARGET"
        print(f"{metric:<20} {score:<10.4f} {target:<10.2f} {status}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"ragas_{args.split}_{timestamp}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp_utc": timestamp,
                "split": args.split,
                "count": len(pairs),
                "scores": scores,
                "targets": TARGET_SCORES,
            },
            f,
            indent=2,
        )
    logger.info("results_written", path=str(output_path))

    if args.gate:
        failures = [m for m, threshold in GATE_THRESHOLDS.items() if scores.get(m, 0.0) < threshold]
        if failures:
            logger.error("ci_gate_failed", failed_metrics=failures, thresholds=GATE_THRESHOLDS)
            sys.exit(1)
        logger.info("ci_gate_passed")


if __name__ == "__main__":
    main()
