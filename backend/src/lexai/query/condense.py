"""
Rewrite a follow-up ("and is it bailable?") into a standalone question using the
recent conversation, so retrieval searches for what the user means. The user
still sees their own words; only the search uses the rewrite.

One short, deterministic model call, made only when it can help: there is
history, and the message leans on it (short, or uses "it/this/that/same",
starts with "and/what about", or names no provision or act of its own).

The rewrite is checked before it is used. A 7B model often "helps" by adding
a section of the old code ("Is it bailable?" after a BNS question became
"...under Section 302 of the Indian Penal Code"), and an added reference is
pinned by the router and changes which code applies. A rewrite may only name
acts the user named and sections that appear in the conversation; otherwise
the search uses the user's previous question followed by the follow-up.
"""

from __future__ import annotations

import re

from lexai.clients.llm import LLMClient
from lexai.core.errors import LexAIError
from lexai.core.logging import get_logger
from lexai.domain.models import ChatMessage
from lexai.ingestion.parsing import normalize_for_match
from lexai.query.references import act_mentions, extract_references

logger = get_logger(__name__)

CONDENSE_PROMPT = """Rewrite the user's LAST message as one standalone question about Indian law, \
using the conversation only to fill in what the last message refers to (the offence, section, act \
or person). Keep every section number and act name. Do not answer it. Do not add anything the user \
did not ask. Output only the rewritten question.

Conversation:
{history}

Last message: {question}

Standalone question:"""

_FOLLOW_UP_CUE = re.compile(
    r"\b(?:it|its|this|that|these|those|they|them|he|she|his|her|same|above|such|there|then|also)\b"
    r"|^\s*(?:and|but|so|what about|how about|what if|why|then|also)\b",
    re.I,
)


def needs_condensing(question: str, history: list[ChatMessage]) -> bool:
    if not any(m.role == "user" for m in history):
        return False
    words = len(question.split())
    self_contained = bool(extract_references(question)) and bool(act_mentions(question))
    if self_contained and words >= 6 and not _FOLLOW_UP_CUE.search(question):
        return False
    return words <= 12 or bool(_FOLLOW_UP_CUE.search(question)) or not act_mentions(question)


_CAP = r"[A-Z][\w()&.-]*"
_ACT_PHRASE = re.compile(rf"\b{_CAP}(?:,?\s+(?:of|and|for|on|the|{_CAP})){{0,8}}?\s+Act\b")
_LEADING = {"is", "are", "under", "what", "which", "does", "do", "the", "in", "as", "per", "of", "for", "and"}


def _ref_keys(text: str) -> set[tuple[str | None, str, str]]:
    return {(r.act, r.unit_label, r.unit) for r in extract_references(text)}


def ungrounded(rewrite: str, question: str, history: list[ChatMessage]) -> list[str]:
    """What the rewrite names that the conversation does not (empty when it is grounded)."""
    users = " ".join([*(m.content for m in history if m.role == "user"), question])
    everything = " ".join([*(m.content for m in history), question])
    problems: list[str] = []
    user_acts = {a for _, _, a in act_mentions(users)}
    for name in sorted({a for _, _, a in act_mentions(rewrite)} - user_acts):
        problems.append(f"act {name}")
    known = _ref_keys(everything)
    units = {(label, unit) for _, label, unit in known}
    for act, label, unit in sorted(_ref_keys(rewrite) - known, key=str):
        if act is None and (label, unit) in units:
            continue  # "section 438" when the conversation said "section 438 of the CrPC"
        problems.append(f"{act or ''} {label} {unit}".strip())
    haystack = normalize_for_match(everything)
    for m in _ACT_PHRASE.finditer(rewrite):
        words = m.group(0).split()
        while len(words) > 2 and words[0].lower() in _LEADING:
            words = words[1:]
        phrase = " ".join(words)
        # "X Act" must occur in the conversation ("Land Acquisition Act" grounds "the Land Acquisition Act").
        if not act_mentions(phrase) and normalize_for_match(" ".join(words[-2:])) not in haystack:
            problems.append(f"unknown act {phrase!r}")
    return problems


def fallback(question: str, history: list[ChatMessage]) -> str:
    """The previous user message followed by the follow-up, verbatim."""
    previous = next((m.content for m in reversed(history) if m.role == "user"), "")
    return f"{previous.strip()} {question.strip()}".strip()


def _clean(text: str) -> str:
    text = text.strip().strip('"').strip()
    text = re.sub(r"^(?:standalone question|question|rewritten question)\s*:\s*", "", text, flags=re.I)
    return text.split("\n")[0].strip().strip('"').strip()


async def condense(
    llm: LLMClient, question: str, history: list[ChatMessage], *, turns: int = 3, max_tokens: int = 96
) -> str:
    """The standalone question, or the original question if rewriting is not needed or fails."""
    if not needs_condensing(question, history):
        return question
    recent = history[-2 * turns :]
    lines = []
    for m in recent:
        content = m.content if m.role == "user" else m.content[:600]
        lines.append(f"{'User' if m.role == 'user' else 'Assistant'}: {content}")
    prompt = CONDENSE_PROMPT.format(history="\n".join(lines), question=question)
    try:
        text = await llm.complete(
            [ChatMessage(role="user", content=prompt)], temperature=0.0, max_tokens=max_tokens
        )
    except LexAIError as exc:
        logger.warning("condense_failed", error=str(exc))
        return question
    rewritten = _clean(text)
    if not rewritten or len(rewritten) > 400:
        return fallback(question, history)
    problems = ungrounded(rewritten, question, history)
    if problems:
        logger.info("condense_rejected", rewrite=rewritten, added=problems)
        return fallback(question, history)
    return rewritten
