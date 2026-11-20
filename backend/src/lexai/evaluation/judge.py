"""
Judged answer metrics (RAGAS-style definitions, implemented directly).

RAGAS itself is not a dependency: it pulls in LangChain, which Phase 1
removed. The metrics follow its definitions, scored by a judge model that is
NOT the generator (a 7B model must not grade itself):

- faithfulness: share of the answer's claims supported by the passages;
- answer_relevancy: does the answer address the question (0-1);
- context_precision: average precision of passages judged useful for the
  reference answer, in rank order;
- context_recall: share of the reference answer's statements found in the passages;
- citation_support: share of cited sentences whose cited passage supports them.

The judge is any OpenAI-compatible endpoint (e.g. Qwen2.5-32B on a rented GPU,
or a hosted API), configured with LEXAI_EVAL_JUDGE_BASE_URL / _MODEL / _API_KEY.
Without it these metrics are reported as not run.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from lexai.clients.llm import LLMClient
from lexai.core.errors import LexAIError
from lexai.core.logging import get_logger
from lexai.core.settings import LLMSettings
from lexai.domain.models import ChatMessage
from lexai.evaluation.dataset import GoldenItem

logger = get_logger(__name__)
PassageText = Callable[[dict[str, Any]], Awaitable[str]]

ANSWER_PROMPT = """You are grading an answer from a legal research assistant against its sources.

Question: {question}

Numbered source passages:
{passages}

Answer:
{answer}

Return ONLY a JSON object:
{{"claims": [{{"claim": "<one factual claim from the answer>", "supported": true/false}}],
 "cited_sentences": [{{"sentence": "<a sentence with [n] markers>", "supported_by_cited": true/false}}],
 "relevancy": <0.0-1.0, how directly the answer addresses the question>}}
A claim is supported only if the passages state it. Ignore the disclaimer line."""

CONTEXT_PROMPT = """You are grading search results for a legal question.

Question: {question}
Reference answer: {reference}

Numbered passages:
{passages}

Return ONLY a JSON object:
{{"useful": [<true/false per passage, in order: does it help produce the reference answer>],
 "reference_statements": [{{"statement": "<a reference statement>", "in_passages": true/false}}]}}"""


def _json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in judge output")
    value: dict[str, Any] = json.loads(match.group(0))
    return value


def _share(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 4) if flags else None


def average_precision(useful: list[bool]) -> float | None:
    hits, total = 0, 0.0
    for i, u in enumerate(useful, start=1):
        if u:
            hits += 1
            total += hits / i
    return round(total / hits, 4) if hits else (0.0 if useful else None)


class Judge:
    def __init__(self, llm: LLMClient, name: str, passage_text: PassageText, max_passages: int = 6) -> None:
        self.llm = llm
        self.name = name
        self.passage_text = passage_text
        self.max_passages = max_passages

    @classmethod
    def from_env(cls, passage_text: PassageText) -> Judge | None:
        base_url = os.environ.get("LEXAI_EVAL_JUDGE_BASE_URL")
        model = os.environ.get("LEXAI_EVAL_JUDGE_MODEL")
        if not base_url or not model:
            return None
        settings = LLMSettings(
            base_url=base_url,
            model=model,
            api_key=os.environ.get("LEXAI_EVAL_JUDGE_API_KEY", "none"),
            temperature=0.0,
            max_tokens=1500,
            read_timeout_s=300,
        )
        return cls(LLMClient(settings), model, passage_text)

    async def _ask(self, prompt: str) -> dict[str, Any] | None:
        try:
            text = await self.llm.complete([ChatMessage(role="user", content=prompt)], temperature=0.0)
            return _json(text)
        except (LexAIError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("judge_failed", error=str(exc))
            return None

    async def score(self, item: GoldenItem, done: dict[str, Any]) -> dict[str, float | None]:
        citations = (done.get("citations") or [])[: self.max_passages]
        texts = [await self.passage_text(c) for c in citations]
        numbered = "\n\n".join(
            f"[{c['ref']}] {c['label']}\n{t[:2500]}" for c, t in zip(citations, texts, strict=True)
        )
        out: dict[str, float | None] = {
            "faithfulness": None,
            "answer_relevancy": None,
            "citation_support": None,
            "context_precision": None,
            "context_recall": None,
        }
        graded = await self._ask(
            ANSWER_PROMPT.format(question=item.question, passages=numbered, answer=done.get("answer", ""))
        )
        if graded:
            out["faithfulness"] = _share([bool(c.get("supported")) for c in graded.get("claims", [])])
            out["citation_support"] = _share(
                [bool(c.get("supported_by_cited")) for c in graded.get("cited_sentences", [])]
            )
            rel = graded.get("relevancy")
            out["answer_relevancy"] = float(rel) if isinstance(rel, int | float) else None
        if item.reference_answer:
            ctx = await self._ask(
                CONTEXT_PROMPT.format(
                    question=item.question, reference=item.reference_answer, passages=numbered
                )
            )
            if ctx:
                out["context_precision"] = average_precision([bool(u) for u in ctx.get("useful", [])])
                out["context_recall"] = _share(
                    [bool(s.get("in_passages")) for s in ctx.get("reference_statements", [])]
                )
        return out
