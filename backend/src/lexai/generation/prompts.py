"""
Prompt templates and context formatting.

The answer prompt lives in `templates/<version>.md` so changes are reviewed as
text; its version is logged with every answer and sent in the `done` event.
"""

from __future__ import annotations

from pathlib import Path

from lexai.domain.models import ChunkMetadata, ScoredChunk

PROMPT_VERSION = "answer_v3"
_TEMPLATES = Path(__file__).parent / "templates"
ANSWER_TEMPLATE = (_TEMPLATES / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def render_system_prompt(context: str, applicable_law: str, notes: list[str]) -> str:
    notes_block = "Notes:\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else ""
    return ANSWER_TEMPLATE.format(context=context, applicable_law=applicable_law, notes=notes_block)


def applicable_law_text(era: str, reason: str) -> str:
    if era == "old":
        return (
            f"The IPC, CrPC and Indian Evidence Act apply ({reason}). Answer under them; mention the "
            "BNS/BNSS/BSA counterpart where a passage gives it."
        )
    return (
        f"The BNS, BNSS and BSA apply ({reason}; they replaced the IPC, CrPC and Indian Evidence Act on "
        "1 July 2024). Answer under them; name the old-code counterpart where a passage gives it, since it "
        "still governs offences committed before that date."
    )


HYDE_PROMPT = """Write a short passage (3-5 sentences) that would answer the legal question below, \
in the style of an Indian statute, judgment or legal commentary. Write only the passage.

Question: {question}"""


def status_text(meta: ChunkMetadata) -> str:
    """Legal status of legislation; empty for judgments and manuals, which are not "in force"."""
    if meta.doc_type in ("judgment", "manual"):
        return ""
    if meta.repealed_on:
        replaced = f"; replaced by {meta.replaced_by.split('-')[0].upper()}" if meta.replaced_by else ""
        return f"repealed {meta.repealed_on}{replaced}; applies to earlier offences"
    return "in force"


def passage_header(n: int, meta: ChunkMetadata) -> str:
    if meta.doc_type == "judgment":
        bits = [f"[{n}] Supreme Court of India: {meta.case_name or meta.doc_title}"]
        if meta.decision_date:
            bits.append(f"decided {meta.decision_date}")
        if meta.citation:
            bits.append(meta.citation)
    else:
        ref = meta.label + (f" — {meta.unit_title}" if meta.unit_title else "")
        if meta.parts > 1:
            ref += f" (part {meta.part} of {meta.parts})"
        bits = [f"[{n}] {ref}", meta.doc_title]
        if meta.doc_type in ("statute", "amendment"):
            bits.append(status_text(meta))
    if meta.page_start is not None:
        bits.append(f"p.{meta.page_start}")
    return " | ".join(bits)


def within_budget(chunks: list[ScoredChunk], char_budget: int) -> list[ScoredChunk]:
    """Keep passages in rank order until the character budget is used (always at least one)."""
    kept: list[ScoredChunk] = []
    used = 0
    for item in chunks:
        size = len(item.chunk.text) + 120  # header allowance
        if kept and used + size > char_budget:
            break
        kept.append(item)
        used += size
    return kept


def _evidence(text: str) -> str:
    """A passage cannot open or close the <passages> block (prompt-injection hardening)."""
    return text.replace("<passages>", "(passages)").replace("</passages>", "(/passages)")


def format_context(chunks: list[ScoredChunk]) -> str:
    return "\n\n".join(
        f"{passage_header(n, c.chunk.metadata)}\n{_evidence(c.chunk.text)}" for n, c in enumerate(chunks, 1)
    )
