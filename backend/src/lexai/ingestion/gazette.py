"""
Gazette of India prints of Acts.

The official Gazette publication of an Act (as distributed by the Legislative
Department, MHA, PRS and eGazette) is laid out differently from India Code:
- no "Arrangement of Sections" and no dash after a heading: a section starts
  `103. (1) Whoever commits murder ...`;
- the section's title is a *marginal note* printed in the outer margin, level
  with the section's first line ("Punishment / for murder.");
- a bilingual masthead on the first page, partly in a legacy Hindi font that
  extracts as gibberish, and a running head on every page.

Text extraction emits the marginal notes as separate short lines, out of
order (top or bottom of the page), so they cannot be matched by position in
the text. Instead we record where each text fragment is drawn: the note whose
first line is level with a section number is that section's title. Notes that
match no section (titles of sections quoted inside an amending Act, citations
like "45 of 1860.") are dropped. The notes are removed from the body text.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pypdf import PageObject

_MASTHEAD_TEST = re.compile(r"PUBLISHED\s+BY\s+AUTHORITY")
_SECTION_MARK = re.compile(r"^\s*(\d{1,3}[A-Z]{0,3})\.(?:\s|$|(?=[A-Z(“\"]))")
_CITATION_NOTE = re.compile(r"^\s*\d{1,3}\s+of\s+\d{4}\.?\s*$")
_RUNNING_HEAD = re.compile(
    r"^\s*(?:\d+\s+)?(?:SEC\.\s*\d+\]\s*)?THE\s+GAZETTE\s+OF\s+INDIA\s+EXTRAORDINARY.*$", re.I
)
_MASTHEAD_LINES = [
    re.compile(p, re.I)
    for p in (
        r"^\s*EXTRAORDINARY\s*$",
        r"^\s*PART\s+II\s*[—–-]\s*Section\s+1\s*$",
        r"^\s*PUBLISHED\s+BY\s+AUTHORITY\s*$",
        r"^\s*No\.\s*\d+\]\s+NEW\s+DELHI",
        r"^\s*Separate\s+paging\s+is\s+given",
        r"^\s*REGISTERED\s+NO\.",
        r"^\s*MINISTRY\s+OF\s+LAW\s+AND\s+JUSTICE\s*$",
        r"^\s*\(Legislative\s+Department\)\s*$",
        r"^\s*New\s+Delhi,\s+the\s+\d",
        r"^\s*The\s+following\s+Act\s+of\s+Parliament\s+received",
        r"^\s*\d{1,2}(?:st|nd|rd|th)\s+\w+,\s+\d{4},?\s+and\s+is\s+hereby\s+published",
        r"^\s*is\s+hereby\s+published\s+for\s+general\s+information",
        r"^\s*CG-DL-E-\d",
        r"^\s*xxxGIDHxxx",
    )
]
# Printer's colophon and the signature of the Legislative Counsel at the end of the Act.
_COLOPHON = re.compile(
    r"^\s*(?:(?:UPLOADED|PRINTED)\s+BY\s+THE\s+MANAGER,\s+GOVERNMENT\s+OF\s+INDIA\s+PRESS"
    r"|AND\s+PUBLISHED\s+BY\s+THE\s+CONTROLLER\s+OF\s+PUBLICATIONS"
    r"|MGIPMRND\s*[—–-]"
    r"|.*(?:Legislative\s+Counsel|Secretary)\s+to\s+the\s+Govt?\.?\s+of\s+India\.?\s*$)",
    re.I,
)
_SIGNATORY = re.compile(r"^\s*[A-Z][A-Z .]{3,40},\s*$")
# Legacy (Krutidev-style) Hindi extracts as Latin gibberish: letters glued to `/ ; [ ] ` @`
# or the characters ª ñ ¼ ½. Only applied to masthead pages.
_LEGACY_HINDI = re.compile(r"[ªñ¼½]|[A-Za-z][/;`@\[\]][A-Za-z]|[A-Za-z]\.[A-Z][a-z]?\b")
_ENGLISH = re.compile(r"\b(?:the|of|and|to|in|shall|be|by|this|act|any|or|which|such)\b", re.I)
MARK_TOLERANCE = 6.0  # points between a section number and its note's first line
NOTE_LINE_GAP = 13.0  # larger vertical gap starts a new note
MAX_NOTE_LINE = 30  # characters; the margin column fits ~20


def is_gazette(first_page_text: str) -> bool:
    return bool(_MASTHEAD_TEST.search(first_page_text)) and "EXTRAORDINARY" in first_page_text


@dataclass
class Fragment:
    x: float
    y: float
    text: str


@dataclass
class PageLayout:
    """Section numbers and marginal notes on one page, with their vertical positions."""

    marks: list[tuple[str, float]] = field(default_factory=list)  # (section number, y)
    notes: list[tuple[float, str]] = field(default_factory=list)  # (y of first line, title)

    def title_for(self, number: str, used: set[int]) -> str | None:
        """Title of the first unused mark for `number` on this page."""
        for i, (num, y) in enumerate(self.marks):
            if num != number or i in used:
                continue
            used.add(i)
            best = min(self.notes, key=lambda n: abs(n[0] - y), default=None)
            if best is not None and abs(best[0] - y) <= MARK_TOLERANCE:
                return best[1]
            return None
        return None


def _fragments(page: PageObject) -> tuple[str, list[Fragment]]:
    frags: list[Fragment] = []

    def visit(text: str, cm: Any, tm: Any, _font: Any, _size: Any) -> None:
        if not text.strip():
            return
        x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
        y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
        frags.append(Fragment(float(x), float(y), text))

    text = page.extract_text(visitor_text=visit) or ""
    return text, frags


def _mode(values: list[float], bucket: float = 4.0) -> float | None:
    """Mean of the values in the most common bucket (the bucket edge can be off by `bucket`)."""
    if not values:
        return None
    key = Counter(round(v / bucket) for v in values).most_common(1)[0][0]
    members = [v for v in values if round(v / bucket) == key]
    return sum(members) / len(members)


_BARE_NUMBER = re.compile(r"^\s*\d{1,3}[A-Z]{0,3}\s*$")


def _join_split_marks(frags: list[Fragment]) -> list[Fragment]:
    """Some prints draw a section number and its period separately: '144' then '. Whoever'."""
    out: list[Fragment] = []
    for f in frags:
        prev = out[-1] if out else None
        if (
            prev is not None
            and _BARE_NUMBER.match(prev.text)
            and f.text.startswith(".")
            and abs(prev.y - f.y) < 1.0
        ):
            out[-1] = Fragment(prev.x, prev.y, prev.text.strip() + f.text)
        else:
            out.append(f)
    return out


def _layout(
    frags: list[Fragment], width: float, body_x: float | None, body_left: float | None
) -> tuple[PageLayout, list[str]]:
    """
    Find section marks and marginal notes; return the layout and the note lines to remove.
    `body_x` (where section numbers start) and `body_left` (the left edge of body text)
    are measured over the whole document: on a single page a numbered list or a page
    without full-width lines at the edge would mislead a per-page guess.
    """
    mark_frags = [f for f in frags if _SECTION_MARK.match(f.text)]
    marks = []
    if body_x is not None:
        for f in mark_frags:
            m = _SECTION_MARK.match(f.text)
            if m and abs(f.x - body_x) <= MARK_TOLERANCE:
                marks.append((m.group(1), f.y))

    # Marginal columns: x positions in the outer fifth of the page used by several short
    # fragments that contain words. The margin is narrow, so a note line is short; a long
    # fragment starting there is body text or the running head.
    def short(f: Fragment) -> bool:
        return all(len(line.strip()) <= MAX_NOTE_LINE for line in f.text.split("\n"))

    # Notes sit clearly left of the body or past its right end (body lines are ~60% of
    # the page wide).
    if body_left is None:
        body_left = 0.2 * width

    def outside_body(x: float) -> bool:
        return x < body_left - 20 or x > body_left + 0.6 * width

    outer = [f for f in frags if outside_body(f.x) and short(f) and sum(c.isalpha() for c in f.text) >= 3]
    # Outside the body there is nothing but notes and citations, so one note is enough.
    columns = list({round(f.x / 4) * 4 for f in outer})
    in_margin = [
        f
        for f in frags
        if outside_body(f.x)
        and short(f)
        and (
            (any(abs(f.x - c) <= 4 for c in columns) and any(c.isalnum() for c in f.text))
            or _CITATION_NOTE.match(f.text)
        )
    ]

    removed: list[str] = []
    groups: list[list[Fragment]] = []
    for f in in_margin:
        for line in f.text.split("\n"):
            if not line.strip():
                continue
            removed.append(line.strip())
            if _CITATION_NOTE.match(line):
                continue
            frag = Fragment(f.x, f.y, line.strip())
            prev = groups[-1][-1] if groups else None
            new_group = (
                prev is None
                or abs(prev.x - frag.x) > 4
                or abs(prev.y - frag.y) > NOTE_LINE_GAP
                or prev.text.endswith(".")
            )
            if new_group:
                groups.append([frag])
            else:
                groups[-1].append(frag)
    notes = []
    for g in groups:
        title = ""
        for frag in g:
            # Gazette notes are not hyphenated by the typesetter: a line ending in "-" is a
            # real compound ("search-/warrant", "non-/treatment"), so keep the hyphen.
            title = title + frag.text if title.endswith("-") else f"{title} {frag.text}".strip()
        notes.append((g[0].y, title.rstrip(".").strip()))
    return PageLayout(marks, notes), removed


def _remove_lines(text: str, remove: list[str], masthead: bool) -> str:
    budget = Counter(remove)
    kept: list[str] = []
    after_designation = False
    for line in text.split("\n"):
        stripped = line.strip()
        if after_designation and _SIGNATORY.match(line):  # name printed after the designation
            after_designation = False
            continue
        after_designation = False
        if budget.get(stripped, 0) > 0:
            budget[stripped] -= 1
            continue
        if _RUNNING_HEAD.match(line):
            continue
        if _COLOPHON.match(line):
            if re.search(r"of\s+India", line):
                if kept and _SIGNATORY.match(kept[-1]):
                    kept.pop()  # the officer's name above the designation
                else:
                    after_designation = True
            continue
        if masthead and any(p.match(line) for p in _MASTHEAD_LINES):
            continue
        if masthead and _LEGACY_HINDI.search(line) and len(_ENGLISH.findall(line)) < 2:
            continue
        kept.append(line)
    return "\n".join(kept)


def extract_gazette_pages(pages: list[PageObject]) -> tuple[list[str], list[PageLayout]]:
    extracted: list[tuple[str, list[Fragment], float] | None] = []
    for page in pages:
        try:
            raw, frags = _fragments(page)
        except Exception:  # malformed page: keep going, the build checks catch thin documents
            extracted.append(None)
            continue
        box = page.mediabox
        left, bottom = float(box.left), float(box.bottom)
        right, top = float(box.right), float(box.top)
        # Some prints draw an invisible copy of the running head off the page (e.g. at
        # y < 0); it would pull the measured body edge to x = 0. Use only what is visible.
        visible = _join_split_marks([f for f in frags if left < f.x < right and bottom <= f.y <= top])
        extracted.append((raw, visible, right - left))

    pages_ok = [e for e in extracted if e]
    body_x = _mode([f.x for e in pages_ok for f in e[1] if _SECTION_MARK.match(f.text)])
    # Left edge of the body: where most full lines start. (Not the minimum: schedule tables
    # and the masthead have wide lines starting further left.)
    body_left = _mode([f.x for e in pages_ok for f in e[1] if any(len(ln) > 40 for ln in f.text.split("\n"))])
    texts: list[str] = []
    layouts: list[PageLayout] = []
    for e in extracted:
        if e is None:
            texts.append("")
            layouts.append(PageLayout())
            continue
        raw, visible, width = e
        layout, note_lines = _layout(visible, width, body_x, body_left)
        masthead = bool(_MASTHEAD_TEST.search(raw))
        texts.append(_remove_lines(raw, note_lines, masthead))
        layouts.append(layout)
    return texts, layouts
