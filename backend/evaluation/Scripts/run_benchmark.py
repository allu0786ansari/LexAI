"""
Retrieval-only benchmark (proposal §7.2/§7.3) against LegalBench-RAG-mini,
producing the ablation table comparing:
  baseline    - FAISS dense retrieval only (the original project's approach)
  hybrid      - FAISS + BM25, fused with RRF
  full        - hybrid + CrossEncoder reranking

Data format matches the real upstream repo exactly (verified against
https://github.com/zeroentropy-cc/legalbenchrag's benchmark_types.py, not
guessed) so a real download drops in without any reshaping:
    data/corpus/<file_path>            - plain text files
    data/benchmarks/<name>.json        - {"tests": [{"query": str, "snippets": [{"file_path": str, "span": [start, end]}], "tags": [...]}]}

Get the real data from the download link in that repo's README (Dropbox,
generated from the same pipeline as the paper) or the Hugging Face mirror
(huggingface.co/datasets/isaacus/legal-rag-bench) named in proposal §11 -
neither could be fetched automatically while building this (Dropbox/HF
aren't reachable from the build environment). Point --benchmark-dir at
wherever you put them.

Precision/recall are computed at the CHARACTER-span level, matching the
upstream formula exactly: for each query, the fraction of a retrieved
chunk's characters that fall inside a ground-truth span (precision), and
the fraction of a ground-truth span's characters that got covered by
retrieval (recall). This is why the ablation harness ingests the
benchmark corpus with its OWN lightweight offset-tracking chunker rather
than the production SemanticChunker: character-span ground truth requires
knowing exactly which document offsets each chunk covers, and the
production chunker (designed for retrieval quality, not benchmark
scoring) doesn't track that. nDCG@10 is our own addition on top of the
upstream metrics (graded relevance = per-chunk character-overlap ratio),
since the upstream repo only computes precision/recall.

Usage:
    python -m evaluation.run_benchmark --benchmark-dir data --benchmarks privacy_qa contractnli
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config.logging import configure_logging, get_logger
from app.config.settings import get_settings
from app.services.bm25_store import Bm25Store
from app.services.vector_store import FaissVectorStore
from langchain_core.documents import Document
from app.services.providers import build_embeddings_client

logger = get_logger(__name__)

CHUNK_SIZE_CHARS = 1000
CHUNK_OVERLAP_CHARS = 100
TOP_K = 10  # retrieve/rerank down to this many chunks per query for scoring


@dataclass
class OffsetChunk:
    file_path: str
    start: int
    end: int
    text: str


@dataclass
class GroundTruthSnippet:
    file_path: str
    start: int
    end: int


@dataclass
class QueryGroundTruth:
    query: str
    snippets: list[GroundTruthSnippet]
    tags: list[str]


def _find_benchmark_file(benchmark_dir: Path, benchmark_name: str) -> Path | None:
    for candidate in [benchmark_dir / "benchmarks" / f"{benchmark_name}.json", benchmark_dir / "queries" / f"{benchmark_name}.json"]:
        if candidate.exists():
            return candidate
    return None


def load_benchmark(benchmark_dir: Path, benchmark_names: list[str]) -> tuple[list[QueryGroundTruth], dict[str, str]]:
    """Loads {name}.json test files + the corpus text files they reference. Schema matches upstream exactly."""
    tests: list[QueryGroundTruth] = []
    referenced_files: set[str] = set()

    for name in benchmark_names:
        path = _find_benchmark_file(benchmark_dir, name)
        if path is None:
            logger.warning("benchmark_file_not_found", benchmark_name=name, path=str(benchmark_dir / "benchmarks" / f"{name}.json"))
            continue
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        for test in raw["tests"]:
            snippets = [GroundTruthSnippet(s["file_path"], s["span"][0], s["span"][1]) for s in test["snippets"]]
            tests.append(QueryGroundTruth(query=test["query"], snippets=snippets, tags=[name]))
            referenced_files.update(s.file_path for s in snippets)

    corpus: dict[str, str] = {}
    for file_path in referenced_files:
        full_path = benchmark_dir / "corpus" / file_path
        if not full_path.exists():
            logger.warning("corpus_file_not_found", path=str(full_path))
            continue
        corpus[file_path] = full_path.read_text(encoding="utf-8")

    logger.info("benchmark_loaded", tests=len(tests), corpus_files=len(corpus))
    return tests, corpus


def chunk_corpus_with_offsets(corpus: dict[str, str]) -> list[OffsetChunk]:
    """
    Simple fixed-size chunking with tracked character offsets. Deliberately
    NOT the production SemanticChunker: this benchmark isolates the
    retrieval algorithm as the variable under test (per proposal §7.3's
    ablation framing — "each upgrade to the retrieval pipeline"), so using
    one simple, consistent chunker across all three configs keeps chunking
    strategy from confounding the comparison. It also needs to track exact
    offsets for character-level scoring, which the production chunker
    (correctly) doesn't bother with since the live app never needs it.
    """
    chunks: list[OffsetChunk] = []
    for file_path, text in corpus.items():
        start = 0
        while start < len(text):
            end = min(start + CHUNK_SIZE_CHARS, len(text))
            chunks.append(OffsetChunk(file_path=file_path, start=start, end=end, text=text[start:end]))
            if end == len(text):
                break
            start = end - CHUNK_OVERLAP_CHARS
    return chunks


def char_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def precision_recall(retrieved: list[OffsetChunk], ground_truth: list[GroundTruthSnippet]) -> tuple[float, float]:
    """Exact character-overlap formula from legalbenchrag's run_benchmark.py QAResult.precision/.recall."""
    total_retrieved_len = sum(c.end - c.start for c in retrieved)
    total_relevant_len = sum(s.end - s.start for s in ground_truth)

    relevant_retrieved_len = 0
    for chunk in retrieved:
        for gt in ground_truth:
            if chunk.file_path == gt.file_path:
                relevant_retrieved_len += char_overlap(chunk.start, chunk.end, gt.start, gt.end)

    precision = relevant_retrieved_len / total_retrieved_len if total_retrieved_len else 0.0
    recall = relevant_retrieved_len / total_relevant_len if total_relevant_len else 0.0
    return precision, recall


def ndcg_at_k(retrieved: list[OffsetChunk], ground_truth: list[GroundTruthSnippet], k: int) -> float:
    """
    Our own addition (upstream doesn't compute nDCG). Graded relevance per
    retrieved chunk = fraction of the chunk covered by ground-truth spans,
    which degrades gracefully to a 0/1 relevance signal when overlap is
    all-or-nothing and rewards partial matches otherwise.
    """
    def relevance(chunk: OffsetChunk) -> float:
        chunk_len = chunk.end - chunk.start
        if chunk_len == 0:
            return 0.0
        overlap = sum(
            char_overlap(chunk.start, chunk.end, gt.start, gt.end)
            for gt in ground_truth
            if gt.file_path == chunk.file_path
        )
        return overlap / chunk_len

    top_k = retrieved[:k]
    dcg = sum(relevance(chunk) / math.log2(i + 2) for i, chunk in enumerate(top_k))

    ideal_relevances = sorted((relevance(c) for c in retrieved), reverse=True)[:k]
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal_relevances))

    return dcg / idcg if idcg > 0 else 0.0


class BenchmarkIndex:
    """Builds an in-memory FAISS+BM25 index over the benchmark corpus (separate from the production Database/)."""

    def __init__(self, chunks: list[OffsetChunk], embeddings_client):
        self.chunks = chunks
        texts = [c.text for c in chunks]
        vectors = embeddings_client.embed_documents(texts)
        docs = [
            Document(page_content=c.text, metadata={"file_path": c.file_path, "start": c.start, "end": c.end})
            for c in chunks
        ]
        self.faiss = FaissVectorStore(dimension=len(vectors[0]))
        self.faiss.add_documents(docs, vectors)
        self.bm25 = Bm25Store(docs)
        self._query_embeddings = embeddings_client

    def _to_offset_chunks(self, docs) -> list[OffsetChunk]:
        return [OffsetChunk(d.metadata["file_path"], d.metadata["start"], d.metadata["end"], d.page_content) for d in docs]

    def retrieve_baseline(self, query: str, k: int) -> list[OffsetChunk]:
        """FAISS dense-only — the original project's approach, before any of this project's upgrades."""
        query_vec = self._query_embeddings.embed_query(query)
        results = self.faiss.similarity_search_by_vector(query_vec, k=k)
        return self._to_offset_chunks([r.document for r in results])

    def retrieve_hybrid(self, query: str, k: int, rrf_k: int = 60) -> list[OffsetChunk]:
        """FAISS + BM25, fused with RRF."""
        from app.services.retriever import reciprocal_rank_fusion

        query_vec = self._query_embeddings.embed_query(query)
        dense = [r.document for r in self.faiss.similarity_search_by_vector(query_vec, k=k * 2)]
        sparse = [r.document for r in self.bm25.search(query, k=k * 2)]
        fused = reciprocal_rank_fusion([dense, sparse], k=rrf_k)
        return self._to_offset_chunks([f.document for f in fused[:k]])

    def retrieve_full(self, query: str, k: int, reranker, rrf_k: int = 60, fused_top_n: int = 20) -> list[OffsetChunk]:
        """Hybrid + CrossEncoder rerank."""
        query_vec = self._query_embeddings.embed_query(query)
        dense = [r.document for r in self.faiss.similarity_search_by_vector(query_vec, k=fused_top_n)]
        sparse = [r.document for r in self.bm25.search(query, k=fused_top_n)]

        from app.services.retriever import reciprocal_rank_fusion

        fused = reciprocal_rank_fusion([dense, sparse], k=rrf_k)
        candidates = [f.document for f in fused[:fused_top_n]]
        reranked = reranker.rerank(query, candidates, top_n=k)
        return self._to_offset_chunks([r.document for r in reranked])


def run_pipeline_config(
    config_name: str,
    tests: list[QueryGroundTruth],
    index: BenchmarkIndex,
    retrieve_fn,
) -> dict:
    precisions, recalls, ndcgs = [], [], []
    for test in tests:
        retrieved = retrieve_fn(test.query)
        p, r = precision_recall(retrieved, test.snippets)
        ndcg = ndcg_at_k(retrieved, test.snippets, k=10)
        precisions.append(p)
        recalls.append(r)
        ndcgs.append(ndcg)

    def avg(xs):
        return sum(xs) / len(xs) if xs else float("nan")

    result = {
        "config": config_name,
        "num_queries": len(tests),
        "precision_at_5": avg(precisions),
        "recall_at_5": avg(recalls),
        "ndcg_at_10": avg(ndcgs),
    }
    logger.info("pipeline_config_complete", **result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "benchmark",
        help="Directory containing benchmarks/ and corpus/ subfolders, or the repo-local evaluation/benchmark directory.",
    )
    parser.add_argument("--benchmarks", nargs="+", default=["privacy_qa", "contractnli"], help="Benchmark JSON names (without .json) to include.")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent.parent / "results")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    tests, corpus = load_benchmark(args.benchmark_dir, args.benchmarks)
    if not tests or not corpus:
        logger.error(
            "no_benchmark_data_found",
            hint=(
                "No benchmark tests or corpus files were found. "
                "Either point --benchmark-dir at a LegalBench-RAG-mini checkout with benchmarks/ and corpus/ folders, "
                "or use the bundled repo-local benchmark data under evaluation/benchmark."
            ),
        )
        return

    chunks = chunk_corpus_with_offsets(corpus)
    logger.info("corpus_chunked", chunks=len(chunks), files=len(corpus))

    # Use local provider for embeddings (Ollama) to avoid external quotas
    raw_embeddings = build_embeddings_client(settings, task_type="retrieval_document")

    class _EmbeddingsWrapper:
        def __init__(self, client):
            self._client = client

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return self._client.embed_documents(texts)

        def embed_query(self, text: str) -> list[float]:
            # Some providers expose a dedicated embed_query method; fallback to embed_documents
            if hasattr(self._client, "embed_query"):
                return self._client.embed_query(text)
            return self._client.embed_documents([text])[0]

    embeddings_client = _EmbeddingsWrapper(raw_embeddings)
    index = BenchmarkIndex(chunks, embeddings_client)

    results = []
    results.append(run_pipeline_config("baseline_faiss_only", tests, index, lambda q: index.retrieve_baseline(q, k=5)))
    results.append(run_pipeline_config("hybrid_faiss_bm25_rrf", tests, index, lambda q: index.retrieve_hybrid(q, k=5)))

    if settings.reranker_enabled:
        from app.services.reranker import CrossEncoderReranker

        reranker = CrossEncoderReranker(settings)
        results.append(
            run_pipeline_config("full_hybrid_plus_reranker", tests, index, lambda q: index.retrieve_full(q, k=5, reranker=reranker))
        )
    else:
        logger.warning("skipping_full_config", reason="RERANKER_ENABLED=false")

    print("\n=== Retrieval Ablation Study (proposal §7.3) ===")
    print(f"{'Configuration':<28} {'Precision@5':<14} {'Recall@5':<12} {'nDCG@10'}")
    for r in results:
        print(f"{r['config']:<28} {r['precision_at_5']:<14.4f} {r['recall_at_5']:<12.4f} {r['ndcg_at_10']:.4f}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"benchmark_{timestamp}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"timestamp_utc": timestamp, "benchmarks": args.benchmarks, "results": results}, f, indent=2)
    logger.info("results_written", path=str(output_path))


if __name__ == "__main__":
    main()
