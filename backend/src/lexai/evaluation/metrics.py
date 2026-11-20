"""
Deterministic metrics (no model judge).

Retrieval is scored at provision level: the passages the model would see are
reduced to keys — a provision label ("BNS s.103") or, for judgments, a doc_id —
in rank order, duplicates dropped. Gold is a set of such keys.

- Hit@k: any gold key in the top k.
- Recall@k: share of gold keys in the top k.
- MRR@k: 1 / rank of the first gold key (0 if none in the top k).
- nDCG@k: binary relevance, ideal = all gold keys first.

Answer metrics read the final answer, its citations and the verification
result: citation validity, gold provision cited, phrases mentioned, code
applied, abstention, refusal and injection resistance.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any

from lexai.evaluation.dataset import GoldenItem
from lexai.ingestion.parsing import normalize_for_match

ABSTAIN_PHRASES = ("do not answer this", "not found in the indexed", "indexed sources do not")
_MARKER = re.compile(r"\[(\d{1,3})\]")


def passage_key(passage: dict[str, Any]) -> str:
    """A citation/passage dict -> scoring key: provision label, or doc_id for judgments."""
    if passage.get("law_type") == "judgment":
        return str(passage["doc_id"])
    return str(passage["label"])


def ranked_keys(passages: Sequence[dict[str, Any]]) -> list[str]:
    keys = [passage_key(p) for p in passages]
    return list(dict.fromkeys(keys))


def gold_keys(item: GoldenItem) -> set[str]:
    return set(item.gold) | set(item.gold_docs)


def retrieval_scores(item: GoldenItem, keys: list[str]) -> dict[str, float]:
    gold = gold_keys(item)
    hits = [k in gold for k in keys]
    first = next((i for i, h in enumerate(hits[:10]) if h), None)
    dcg = sum(1 / math.log2(i + 2) for i, h in enumerate(hits[:10]) if h)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), 10)))
    return {
        "hit@1": float(any(hits[:1])),
        "hit@5": float(any(hits[:5])),
        "recall@8": sum(hits[:8]) / len(gold),
        "mrr@10": 1 / (first + 1) if first is not None else 0.0,
        "ndcg@10": dcg / ideal if ideal else 0.0,
    }


def mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else float("nan")


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}
    return {k: mean([r[k] for r in rows]) for k in rows[0]}


def predicted_abstain(done: dict[str, Any]) -> bool:
    if done.get("abstained"):
        return True
    text = normalize_for_match(str(done.get("answer", "")))
    return any(p in text for p in ABSTAIN_PHRASES)


def answer_scores(item: GoldenItem, events: dict[str, Any]) -> dict[str, float | None]:
    """Scores for one generated answer (`events`: the last event of each SSE type)."""
    done = events.get("done") or {}
    meta = events.get("meta") or {}
    verification = events.get("verification") or done.get("verification") or {}
    answer = str(done.get("answer", ""))
    norm = normalize_for_match(answer)
    citations = done.get("citations") or []
    cited_refs = {int(n) for n in _MARKER.findall(answer)}
    cited_keys = {passage_key(c) for c in citations if c.get("ref") in cited_refs}
    gold = gold_keys(item)
    abstained = predicted_abstain(done)
    markers = len(_MARKER.findall(answer)) + len(verification.get("removed_markers") or [])

    scores: dict[str, float | None] = {
        "answered": float(bool(answer) and not abstained and meta.get("intent") != "refused"),
        "abstained": float(abstained),
        "refused": float(meta.get("intent") == "refused"),
        "verified": float(bool(verification.get("valid"))) if not abstained else None,
        "coverage": verification.get("coverage"),
        "citation_validity": (
            1 - len(verification.get("removed_markers") or []) / markers if markers else None
        ),
        "gold_cited": (float(bool(gold & cited_keys)) if gold and not item.should_abstain else None),
        "must_mention": (
            sum(normalize_for_match(p) in norm for p in item.must_mention) / len(item.must_mention)
            if item.must_mention and not abstained
            else None
        ),
        "must_not_mention_ok": (
            float(not any(normalize_for_match(p) in norm for p in item.must_not_mention))
            if item.must_not_mention
            else None
        ),
        "code_correct": (
            float(meta.get("applicable_code") == item.applicable_code) if item.applicable_code else None
        ),
        "ttft_ms": (done.get("latency") or {}).get("time_to_first_token_ms"),
        "total_ms": (done.get("latency") or {}).get("total_ms"),
    }
    return scores


def abstention_pr(items: list[GoldenItem], predicted: list[bool]) -> dict[str, float]:
    """Precision/recall of abstaining, over answerable + unanswerable items."""
    tp = sum(p and i.should_abstain for i, p in zip(items, predicted, strict=True))
    fp = sum(p and not i.should_abstain for i, p in zip(items, predicted, strict=True))
    fn = sum((not p) and i.should_abstain for i, p in zip(items, predicted, strict=True))
    return {
        "abstention_precision": round(tp / (tp + fp), 4) if tp + fp else float("nan"),
        "abstention_recall": round(tp / (tp + fn), 4) if tp + fn else float("nan"),
        "false_abstentions": float(fp),
    }


def summarise_answers(rows: list[dict[str, float | None]]) -> dict[str, float]:
    keys = rows[0].keys() if rows else []
    out: dict[str, float] = {}
    for k in keys:
        values = [float(v) for r in rows if (v := r[k]) is not None]
        if values:
            out[k] = mean(values)
            out[f"{k}_n"] = float(len(values))
    return out
