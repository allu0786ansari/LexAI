"""
Find references to statutory provisions in a question: "302 IPC", "Section 438 of
the CrPC", "BNS s.103", "Art. 19(1)(a)", "sections 154 and 156 CrPC".

Rules only (no model call): the forms lawyers write are few and regular, and a
rule that misses is cheaper to notice and fix than a model that guesses. The
act names map to the `short_name`s in data/manifest.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# short_name -> ways people write it (lowercase). Longest aliases are matched first.
ACT_ALIASES: dict[str, list[str]] = {
    "BNS": ["bns", "bharatiya nyaya sanhita", "bhartiya nyaya sanhita", "nyaya sanhita"],
    "BNSS": [
        "bnss",
        "bharatiya nagarik suraksha sanhita",
        "bhartiya nagarik suraksha sanhita",
        "nagarik suraksha sanhita",
    ],
    "BSA": ["bsa", "bharatiya sakshya adhiniyam", "bhartiya sakshya adhiniyam", "sakshya adhiniyam"],
    "IPC": ["ipc", "i.p.c.", "i.p.c", "indian penal code", "penal code"],
    "CrPC": [
        "crpc",
        "cr.p.c.",
        "cr.p.c",
        "cr. p.c.",
        "code of criminal procedure",
        "criminal procedure code",
    ],
    "IEA": ["iea", "indian evidence act", "evidence act"],
    "Constitution": ["constitution of india", "indian constitution", "constitution"],
    "IT Act": ["information technology act", "it act", "i.t. act"],
    "Prisons Act": ["prisons act"],
    "CLA 2018": ["criminal law (amendment) act, 2018", "criminal law amendment act 2018", "cla 2018"],
}
OLD_CODES = {"IPC": "BNS", "CrPC": "BNSS", "IEA": "BSA"}
NEW_CODES = {new: old for old, new in OLD_CODES.items()}

_ALIAS_TO_ACT = {alias: act for act, aliases in ACT_ALIASES.items() for alias in aliases}
_ACT = "|".join(re.escape(a) for a in sorted(_ALIAS_TO_ACT, key=len, reverse=True))
_ACT_RE = re.compile(rf"(?<![a-z])(?:{_ACT})(?![a-z])", re.I)
_NUM = r"\d{1,3}[A-Za-z]{0,3}"
_SUB = r"(?:\s*\(\s*[0-9a-zA-Z]{1,4}\s*\))*"
_KIND = r"(?:sections?|secs?\.?|ss?\.|u/s\.?|articles?|arts?\.?)"
_LIST_TAIL = rf"(?:\s*(?:,|and|or|&|/)\s*{_NUM}{_SUB})*"
# "section 302", "s. 302", "u/s 438", "Article 19(1)(a)", "sections 154, 156 and 157"
_KEYWORD_RE = re.compile(rf"(?<![a-z])(?P<kind>{_KIND})\s*(?P<nums>{_NUM}{_SUB}{_LIST_TAIL})(?![\w])", re.I)
# "302 IPC", "438 of the CrPC", "65B Evidence Act"
_NUM_ACT_RE = re.compile(
    rf"(?<![\w./])(?P<nums>{_NUM}{_SUB})\s+(?:of\s+(?:the\s+)?)?(?P<act>{_ACT})(?![a-z])", re.I
)
# "IPC 302", "BNS s.103", "IPC section 302"
_ACT_NUM_RE = re.compile(
    rf"(?<![a-z])(?P<act>{_ACT})\s*,?\s*(?:{_KIND}\s*)?(?P<nums>{_NUM}{_SUB}{_LIST_TAIL})(?![\w])", re.I
)
_ONE_NUM = re.compile(rf"(?P<num>{_NUM})(?P<sub>{_SUB})")


@dataclass(frozen=True)
class Reference:
    act: str | None  # short_name, or None when the act is not stated
    unit_label: str  # "Section" or "Article"
    unit: str  # "302", "65B", "19"
    sub: str = ""  # "(1)(a)"

    @property
    def label(self) -> str:
        prefix = f"{self.act} " if self.act else ""
        mark = "Art." if self.unit_label == "Article" else "s."
        return f"{prefix}{mark}{self.unit}{self.sub}"


def act_mentions(text: str) -> list[tuple[int, int, str]]:
    """(start, end, short_name) of every act named in the text."""
    return [(m.start(), m.end(), _ALIAS_TO_ACT[m.group(0).lower()]) for m in _ACT_RE.finditer(text)]


def _numbers(raw: str) -> list[tuple[str, str]]:
    out = []
    for m in _ONE_NUM.finditer(raw):
        num = m.group("num").upper()
        if num[0] == "0":
            continue
        sub = re.sub(r"\s+", "", m.group("sub"))
        out.append((num, sub))
    return out


def extract_references(text: str) -> list[Reference]:
    acts = act_mentions(text)
    distinct = {a for _, _, a in acts}
    refs: list[Reference] = []
    taken: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < e and s < span[1] for s, e in taken)

    def add(act: str | None, kind: str, nums: str, span: tuple[int, int]) -> None:
        taken.append(span)
        article = kind.lower().startswith("art") or act == "Constitution"
        if article and act is None:
            act = "Constitution"
        for num, sub in _numbers(nums):
            refs.append(Reference(act, "Article" if article else "Section", num, sub))

    # Keyword forms first: they understand lists ("sections 154 and 156 CrPC").
    for m in _KEYWORD_RE.finditer(text):
        # The act is the one written just before ("IPC section 420", "IT Act, section 66C"),
        # else just after ("section 302 of the IPC"), else the only act named anywhere.
        before = [a for _, end, a in acts if 0 <= m.start() - end <= 2]
        after = [a for start, _, a in acts if 0 <= start - m.end() <= 40]
        if before:
            act: str | None = before[-1]
        elif after:
            act = after[0]
        else:
            act = next(iter(distinct)) if len(distinct) == 1 else None
        start = max(s for s, e, _ in acts if 0 <= m.start() - e <= 2) if before else m.start()
        add(act, m.group("kind"), m.group("nums"), (start, m.end()))

    for rx in (_ACT_NUM_RE, _NUM_ACT_RE):
        for m in rx.finditer(text):
            if not overlaps(m.span()):
                add(_ALIAS_TO_ACT[m.group("act").lower()], "section", m.group("nums"), m.span())

    seen: dict[Reference, None] = {}
    for r in refs:
        seen.setdefault(r, None)
    return list(seen)
