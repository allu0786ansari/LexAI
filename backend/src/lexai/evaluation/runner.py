"""
Run the golden set through LexAI in-process (the same code path as the API).

- Retrieval: `ChatService.search` per item (no answer generated). Fast enough
  for every configuration in the ablation table.
- Answers: `ChatService.stream_answer` per item; deterministic answer metrics,
  plus judge metrics when a judge model is configured.

Multi-turn items replay their earlier turns into a session first, so the
follow-up is rewritten exactly as it would be in a conversation.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lexai.core.settings import Settings
from lexai.evaluation import metrics
from lexai.evaluation.dataset import GoldenItem
from lexai.evaluation.judge import Judge
from lexai.generation.service import ChatService
from lexai.services.sessions import SessionStore

PREVIOUS_ANSWER = "(previous answer)"


@dataclass
class ItemResult:
    item: GoldenItem
    keys: list[str] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {
            "id": self.item.id,
            "category": self.item.category,
            "question": self.item.question,
            "gold": self.item.gold + self.item.gold_docs,
            "top": self.keys[:10],
            **self.scores,
            **self.info,
        }


@dataclass
class RunResult:
    config: str
    split: str
    results: list[ItemResult]
    summary: dict[str, Any]
    seconds: float


def _session(sessions: SessionStore, item: GoldenItem) -> str | None:
    if not item.turns:
        return None
    session = sessions.create()
    for turn in item.turns:
        sessions.append_turn(session, turn, PREVIOUS_ANSWER)
    return session


def describe_settings(settings: Settings) -> dict[str, Any]:
    return {
        "llm": settings.llm.model,
        "prompt": settings.llm.prompt_version,
        "embedder": settings.embed.model,
        "reranker": settings.rerank.model if settings.rerank.enabled else None,
        "mode": settings.retrieval.mode,
        "routing": settings.query.routing_enabled,
        "hyde": settings.retrieval.hyde_enabled,
        "condense": settings.query.condense_enabled,
        "abstain_min_dense": settings.query.abstain_min_dense,
        "context_char_budget": settings.retrieval.context_char_budget,
        "rerank_top_n": settings.rerank.top_n,
    }


async def run_retrieval(
    chat: ChatService, sessions: SessionStore, items: list[GoldenItem], config: str, split: str
) -> RunResult:
    start = time.perf_counter()
    results: list[ItemResult] = []
    for item in items:
        res = await chat.search(item.question, _session(sessions, item))
        keys = metrics.ranked_keys(res.get("passages", []))
        r = ItemResult(item, keys)
        if item.has_gold:
            r.scores = metrics.retrieval_scores(item, keys)
        r.info = {
            "standalone": res["plan"]["standalone_query"],
            "intent": res["plan"]["intent"],
            "applicable_code": res["plan"]["applicable_code"],
            "best_dense": res.get("best_dense"),
            "abstain": bool(res.get("abstain")) or res["plan"]["intent"] == "out_of_scope",
            "refused": res["plan"]["intent"] == "refused",
        }
        results.append(r)
    return RunResult(
        config, split, results, summarise_retrieval(results), round(time.perf_counter() - start, 1)
    )


def summarise_retrieval(results: list[ItemResult]) -> dict[str, Any]:
    scored = [r for r in results if r.scores]
    by_cat: dict[str, list[dict[str, float]]] = {}
    for r in scored:
        by_cat.setdefault(r.item.category, []).append(r.scores)
    answerable = [r for r in results if not r.item.should_abstain and not r.item.expect_refusal]
    abstention = metrics.abstention_pr(
        [r.item for r in results if not r.item.expect_refusal],
        [r.info["abstain"] for r in results if not r.item.expect_refusal],
    )
    refusals = [r for r in results if r.item.expect_refusal]
    code_items = [r for r in results if r.item.applicable_code]
    return {
        "items": len(results),
        "scored": len(scored),
        "overall": metrics.aggregate([r.scores for r in scored]),
        "by_category": {c: metrics.aggregate(rows) | {"n": len(rows)} for c, rows in sorted(by_cat.items())},
        "abstention": abstention | {"answerable_items": len(answerable)},
        "refusal_recall": metrics.mean([float(r.info["refused"]) for r in refusals]) if refusals else None,
        "refusal_false_positives": sum(r.info["refused"] for r in results if not r.item.expect_refusal),
        "code_correct": (
            metrics.mean([float(r.info["applicable_code"] == r.item.applicable_code) for r in code_items])
            if code_items
            else None
        ),
        "intents": dict(Counter(r.info["intent"] for r in results)),
    }


async def run_answers(
    chat: ChatService, sessions: SessionStore, items: list[GoldenItem], split: str, judge: Judge | None = None
) -> RunResult:
    start = time.perf_counter()
    results: list[ItemResult] = []
    for item in items:
        session = _session(sessions, item) or sessions.create()
        events: dict[str, Any] = {}
        async for event in chat.stream_answer(item.question, session, uuid.uuid4().hex):
            if event.event == "token":
                continue
            events[event.event] = event.data
        done = events.get("done") or {}
        r = ItemResult(item, metrics.ranked_keys(done.get("citations") or []))
        r.scores = dict(metrics.answer_scores(item, events))
        if judge is not None and done.get("answer") and not done.get("abstained"):
            r.scores.update(await judge.score(item, done))
        r.info = {
            "answer": done.get("answer", ""),
            "error": (events.get("error") or {}).get("code"),
            "verification_flags": (events.get("verification") or {}).get("flags"),
            "standalone": (events.get("meta") or {}).get("standalone_query"),
        }
        results.append(r)
    rows = [r.scores for r in results]
    summary: dict[str, Any] = {"items": len(results), "overall": metrics.summarise_answers(rows)}
    summary["abstention"] = metrics.abstention_pr(
        [r.item for r in results if not r.item.expect_refusal],
        [bool(r.scores.get("abstained")) for r in results if not r.item.expect_refusal],
    )
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_cat.setdefault(r.item.category, []).append(r.scores)
    summary["by_category"] = {c: metrics.summarise_answers(v) for c, v in sorted(by_cat.items())}
    summary["errors"] = sum(1 for r in results if r.info["error"])
    summary["judge"] = judge.name if judge else None
    return RunResult("answers", split, results, summary, round(time.perf_counter() - start, 1))
