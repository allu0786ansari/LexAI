"""
Understand a (standalone) question before searching: which provisions it names,
what kind of question it is, which code applies, and whether to answer at all.

All rules, no model call: each decision is cheap, explainable and testable, and
is shown to the client in the `meta` event. Phase 4's evaluation decides whether
a model-based classifier for the unclear cases is worth its latency.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from lexai.query.references import NEW_CODES, OLD_CODES, Reference, act_mentions, extract_references

Intent = Literal["section_lookup", "old_to_new_mapping", "case_law", "concept", "out_of_scope", "refused"]
Era = Literal["old", "new"]
NEW_CODES_FROM = date(2024, 7, 1)

REFUSAL = (
    "I can't help with that. I can explain the law that applies — for example the offence, "
    "its punishment, or a person's rights and remedies such as bail or anticipatory bail."
)
OUT_OF_SCOPE = (
    "I answer questions about Indian law from the indexed statutes, the Constitution and Supreme "
    "Court judgments. Ask about an offence, a procedure, evidence, or a constitutional right."
)

# Help with committing or covering up a crime (not: understanding the law, or lawful
# defences like anticipatory bail). Matched on lower-cased text.
_CRIME_HELP = [
    re.compile(p)
    for p in (
        r"\b(?:destroy|tamper with|get rid of|hide|wipe|delete|plant|fabricate|fake|forge)\b.{0,30}\b(?:evidence|cctv|records?|documents?|fingerprints?|alibi|body|weapon)\b",
        r"\b(?:bribe|pay off)\b.{0,30}\b(?:judge|police|officer|witness|inspector|magistrate|official)\b",
        r"\b(?:threaten|intimidate|silence|scare)\b.{0,30}\b(?:witness|victim|complainant|informant)\b",
        r"\b(?:get away with|not get caught|without getting caught|without being caught|avoid getting caught)\b",
        r"\b(?:hide|dispose of|get rid of)\b.{0,20}\b(?:a|the)?\s*(?:dead )?body\b",
        r"\b(?:escape|flee|run away)\b.{0,30}\b(?:custody|jail|prison|from (?:the )?police|the country)\b.{0,40}\b(?:trial|case|arrest|charges?)?",
        r"\b(?:launder|hide)\b.{0,20}\b(?:money|cash|proceeds)\b",
        r"\bhow (?:do|can|should) i\b.{0,40}\b(?:hack|stalk|blackmail|extort|kidnap|poison)\b",
    )
]
_GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|ok|okay|good (?:morning|evening|afternoon))\W*$", re.I
)
# Words that make a question legal; if none appear and a clearly non-legal topic does,
# the question is out of scope. Anything else goes to retrieval (and abstention).
_LEGAL = re.compile(
    r"\b(?:law|legal|illegal|act|section|article|clause|court|judge|judgment|bench|police|fir|bail|arrest|"
    r"offen[cs]es?|crime|criminal|punish\w*|penalt\w*|sentence|prison|jail|custody|rights?|constitution\w*|"
    r"evidence|witness|accused|complain\w*|case|lawyer|advocate|trial|appeal|petition|warrant|summons|"
    r"murder|theft|rape|assault|hurt|cheat\w*|fraud|forgery|defamation|dowry|cruelty|kidnap\w*|robbery|"
    r"cyber|hacking|stalking|harass\w*|bribe\w*|corruption|confession|investigation|magistrate|"
    r"ipc|bns|bnss|bsa|crpc|sanhita|adhiniyam|statute|provision|liable|liability|guilty|acquit\w*|"
    r"conviction|convict\w*|parole|remand|chargesheet|charge[- ]?sheet|compensation|victim|minor|"
    r"marriage|divorce|property|contract|tax|gst|consumer|tenant|landlord|employer|employee)\b",
    re.I,
)
_NON_LEGAL = re.compile(
    r"\b(?:recipe|cook|weather|cricket score|movie|song|lyrics|poem|joke|python|javascript|code|"
    r"programming|math|equation|translate|stock price|horoscope|game)\b",
    re.I,
)
_MAPPING = re.compile(
    r"\b(?:replac\w*|equivalent|correspond\w*|counterpart|new (?:law|code|section)|old (?:law|code|section)|"
    r"now under|under the new|renumbered|in place of|instead of|mapped|same as)\b",
    re.I,
)
_CASE_LAW = re.compile(
    r"\b(?:judgment|judgement|held|holding|ruling|ruled|precedent|case law|supreme court|high court|verdict|"
    r"bench|ratio)\b|\bv(?:s)?\.?\s+(?:state|union)\b",
    re.I,
)
# Dates of the events in the question (not the year of an Act's name).
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_DAY_MONTH_YEAR = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTHS})\.?,?\s+(\d{{4}})\b", re.I)
_MONTH_YEAR = re.compile(rf"\b({_MONTHS})\.?,?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(\d{{4}})\b", re.I)
_EVENT_YEAR = re.compile(
    r"\b(?:in|on|during|since|before|after|by|dated|year|around|early|late|mid)\s+(?:the\s+)?(?:year\s+)?((?:19|20)\d{2})\b",
    re.I,
)
_BEFORE_SWITCH = re.compile(r"\bbefore\s+(?:1(?:st)?\s+)?july\s*,?\s*2024\b|\bold (?:law|code)s?\b", re.I)
_AFTER_SWITCH = re.compile(
    r"\b(?:after|since|from)\s+(?:1(?:st)?\s+)?july\s*,?\s*2024\b|\bnew (?:law|code)s?\b", re.I
)


@dataclass
class EraDecision:
    era: Era
    reason: str
    explicit: bool  # stated by the user (date or code named) rather than defaulted


@dataclass
class QueryPlan:
    question: str  # what the user typed
    standalone: str  # what is searched (the rewritten follow-up, or the question)
    references: list[Reference] = field(default_factory=list)
    acts: list[str] = field(default_factory=list)  # every act named, in order
    intent: Intent = "concept"
    era: EraDecision = field(default_factory=lambda: EraDecision("new", "default", False))
    reply: str | None = None  # set for refused / out-of-scope: answer without searching

    def summary(self) -> dict[str, object]:
        return {
            "standalone_query": self.standalone,
            "intent": self.intent,
            "references": [r.label for r in self.references],
            "applicable_code": self.era.era,
            "applicable_code_reason": self.era.reason,
        }


_MONTH_NUM = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}


def _event_dates(text: str) -> list[date]:
    months = _MONTH_NUM
    found: list[date] = []

    def safe(y: int, m: int, d: int) -> None:
        with contextlib.suppress(ValueError):  # e.g. 31/02/2024
            found.append(date(y, m, d))

    for d, m, y in _DMY.findall(text):
        safe(int(y), int(m), int(d))
    for d, mon, y in _DAY_MONTH_YEAR.findall(text):
        safe(int(y), months[mon.lower()], int(d))
    for mon, y in _MONTH_YEAR.findall(text):
        safe(int(y), months[mon.lower()], 15)
    for (y,) in [(m.group(1),) for m in _EVENT_YEAR.finditer(text)]:
        safe(int(y), 6 if int(y) == 2024 else 7, 1)  # a bare "in 2024" is ambiguous: treat as before
    return found


def decide_era(text: str, acts: list[str]) -> EraDecision:
    if _BEFORE_SWITCH.search(text):
        return EraDecision("old", "the question refers to the law before 1 July 2024", True)
    if _AFTER_SWITCH.search(text):
        return EraDecision("new", "the question refers to the law from 1 July 2024", True)
    dates = _event_dates(text)
    if dates:
        if any(d < NEW_CODES_FROM for d in dates):
            first = min(dates).isoformat()
            return EraDecision("old", f"the events date from {first}, before 1 July 2024", True)
        return EraDecision("new", "the events date from 1 July 2024 or later", True)
    old = [a for a in acts if a in OLD_CODES]
    new = [a for a in acts if a in NEW_CODES]
    if old and not new:
        return EraDecision("old", f"the question names the {', '.join(dict.fromkeys(old))}", True)
    if new and not old:
        return EraDecision("new", f"the question names the {', '.join(dict.fromkeys(new))}", True)
    return EraDecision("new", "no offence date given: the BNS, BNSS and BSA are in force", False)


def classify(text: str, refs: list[Reference], acts: list[str]) -> Intent:
    lowered = text.lower()
    if any(p.search(lowered) for p in _CRIME_HELP):
        return "refused"
    if _GREETING.match(text) or (not _LEGAL.search(text) and not refs and _NON_LEGAL.search(text)):
        return "out_of_scope"
    old_ref = any(r.act in OLD_CODES for r in refs)
    new_ref = any(r.act in NEW_CODES for r in refs)
    names_both = any(a in OLD_CODES for a in acts) and any(a in NEW_CODES for a in acts)
    if (old_ref or new_ref) and (_MAPPING.search(text) or names_both):
        return "old_to_new_mapping"
    if refs:
        return "section_lookup"
    if _CASE_LAW.search(text):
        return "case_law"
    return "concept"


def plan_query(question: str, standalone: str | None = None) -> QueryPlan:
    text = standalone or question
    refs = extract_references(text)
    acts = [a for _, _, a in act_mentions(text)]
    plan = QueryPlan(question=question, standalone=text, references=refs, acts=acts)
    plan.intent = classify(text, refs, acts)
    plan.era = decide_era(text, acts + [r.act for r in refs if r.act])
    if plan.intent == "refused":
        plan.reply = REFUSAL
    elif plan.intent == "out_of_scope":
        plan.reply = OUT_OF_SCOPE
    return plan
