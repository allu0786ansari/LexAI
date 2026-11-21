"""
The golden dataset: questions with the provisions or judgments that answer them.

Gold is written as the labels LexAI uses ("BNS s.103", "Constitution Art.21",
"BNSS Schedule First") or, for case law, a judgment's doc_id. Every item is
validated against the indexed text before it is scored: its gold provisions
must exist, and its `must_mention` phrases must appear in the gold text. An item
that fails validation is excluded and reported, never silently scored.

Items are split 60/40 into dev (for tuning) and test (reported once per
release) by a hash of the item id within each category, so the split never
depends on file order. The test items are locked by a checksum in
`<name>.lock.json`; changing them makes loading fail.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from lexai.core.errors import ConfigurationError
from lexai.ingestion.parsing import normalize_for_match

Category = Literal[
    "concept", "section_lookup", "case_law", "old_to_new", "unanswerable", "multi_turn", "adversarial"
]
DEV_SHARE = 0.6
_LABEL = re.compile(r"^(?P<act>.+?) (?:(?P<kind>s\.|Art\.)(?P<unit>[0-9A-Z]+)|Schedule (?P<sched>\w+))$")


class GoldenItem(BaseModel):
    id: str
    category: Category
    question: str
    turns: list[str] = Field(default_factory=list, description="Earlier user messages (multi-turn).")
    gold: list[str] = Field(default_factory=list, description='Provision labels, e.g. "BNS s.103".')
    gold_docs: list[str] = Field(default_factory=list, description="Judgment doc_ids (case law).")
    must_mention: list[str] = Field(default_factory=list, description="Phrases a good answer contains.")
    must_not_mention: list[str] = Field(default_factory=list)
    reference_answer: str = ""
    should_abstain: bool = False
    expect_refusal: bool = False
    applicable_code: Literal["new", "old"] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> GoldenItem:
        if self.category in ("unanswerable",) and not self.should_abstain:
            raise ValueError(f"{self.id}: unanswerable items must set should_abstain")
        needs_gold = self.category in ("concept", "section_lookup", "old_to_new", "multi_turn")
        if needs_gold and not self.gold:
            raise ValueError(f"{self.id}: needs gold provisions")
        if self.category == "case_law" and not self.gold_docs:
            raise ValueError(f"{self.id}: needs gold_docs")
        for label in self.gold:
            if not _LABEL.match(label):
                raise ValueError(f"{self.id}: bad gold label {label!r}")
        return self

    @property
    def has_gold(self) -> bool:
        return bool(self.gold or self.gold_docs)

    @property
    def split(self) -> str:
        return split_of(self)


def split_of(item: GoldenItem) -> str:
    h = int(hashlib.sha256(f"{item.category}:{item.id}".encode()).hexdigest()[:8], 16)
    return "dev" if (h % 1000) / 1000 < DEV_SHARE else "test"


def parse_label(label: str) -> tuple[str, str, str]:
    """ "BNS s.103" -> ("BNS", "Section", "103"); "BNSS Schedule First" -> ("BNSS", "Schedule", "First")."""
    m = _LABEL.match(label)
    if not m:
        raise ValueError(f"bad label {label!r}")
    if m.group("sched"):
        return m.group("act"), "Schedule", m.group("sched")
    return m.group("act"), "Article" if m.group("kind") == "Art." else "Section", m.group("unit")


def _canonical(items: list[GoldenItem]) -> str:
    return json.dumps([i.model_dump() for i in sorted(items, key=lambda i: i.id)], sort_keys=True)


def load_items(path: Path) -> list[GoldenItem]:
    items = [
        GoldenItem.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise ConfigurationError(f"{path.name}: duplicate item ids")
    return items


def lock_path(path: Path) -> Path:
    return path.with_suffix(".lock.json")


def test_checksum(items: list[GoldenItem]) -> str:
    return hashlib.sha256(_canonical([i for i in items if i.split == "test"]).encode()).hexdigest()


def write_lock(path: Path, items: list[GoldenItem]) -> None:
    test = [i for i in items if i.split == "test"]
    lock_path(path).write_text(
        json.dumps(
            {
                "dataset": path.name,
                "test_items": len(test),
                "test_sha256": test_checksum(items),
                "note": "The test split is reported once per release; do not edit or tune on it.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def load_dataset(path: Path, split: str | None = None) -> list[GoldenItem]:
    """Load, check the test lock, and return the items of `split` (dev / test / None = all)."""
    items = load_items(path)
    lock = lock_path(path)
    if lock.exists():
        expected = json.loads(lock.read_text(encoding="utf-8"))["test_sha256"]
        if test_checksum(items) != expected:
            raise ConfigurationError(
                f"The locked test split of {path.name} has changed. Test items must not be edited; "
                "add new items instead (they are split automatically)."
            )
    return [i for i in items if split in (None, "all") or i.split == split]


# ---------------------------------------------------------------- validation against the source text

TextSource = Callable[[str], Awaitable[str | None]]  # label or doc_id -> text, None if absent


@dataclass
class Validation:
    item: GoldenItem
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


async def validate_item(item: GoldenItem, source: TextSource) -> Validation:
    problems: list[str] = []
    texts: list[str] = []
    for ref in [*item.gold, *item.gold_docs]:
        text = await source(ref)
        if text is None:
            problems.append(f"{ref} not found in the index")
        else:
            texts.append(text)
    if texts and item.category not in ("unanswerable", "adversarial"):
        haystack = normalize_for_match("\n".join(texts))
        for phrase in item.must_mention:
            if normalize_for_match(phrase) not in haystack:
                problems.append(f"must_mention {phrase!r} is not in the gold text")
    return Validation(item, problems)
