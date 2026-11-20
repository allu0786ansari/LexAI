"""
Legal-structure chunking.

Statutes and the Constitution are split into their own units — one chunk per
section or article — instead of fixed-size windows. This keeps "Section 302"
together, lets a question about it retrieve exactly that text, and gives every
chunk a precise citation (act, section, title, chapter, pages).

How a statute is read:
1. The body starts at the first heading `1. <title>.—<text>`. The "Arrangement
   of Sections" at the front lists the same titles but without the dash and the
   text after it, so it is never mistaken for the body.
2. Every later heading `<n>. <title>.—` starts a new unit if its number follows
   the previous one (e.g. 302 -> 303 or 21 -> 21A). Out-of-order numbers (a
   numbered list inside a section) stay part of the current unit.
3. CHAPTER / PART headings set the hierarchy shown in citations.
4. Text before section 1 becomes the "Preamble" unit; text from the first
   SCHEDULE heading onwards becomes "Schedule" units.
5. A unit longer than `max_chunk_chars` is split at paragraph boundaries; each
   piece repeats the unit header and shares a `parent_id`.

Judgments are chunked into a headnote and the judgment body, with case name,
date, bench and citation read from the JUDIS header. Manuals are split by size.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from lexai.domain.models import Chunk, ChunkMetadata
from lexai.ingestion.manifest import DocumentSpec
from lexai.ingestion.parsing import DocumentText, extract_sections, normalize_for_match, split_text

CHUNKER_VERSION = "legal-v1"
_NAMESPACE = uuid.UUID("5b0c8a2e-6f1d-4c7a-9a43-1f2e3d4c5b6a")

_HEADING = re.compile(
    r"(?m)^[ \t]*\[?(?P<num>\d{1,3}[A-Z]{0,3})\.[ \t]+"
    r"(?P<title>[^\n]{1,220}?(?:\n[^\n]{1,160}?)?)"
    r"(?:\.[ \t]*(?:—|–|-{1,2})|[ \t]*[—–])"
)
_DIVISION = re.compile(
    r"(?m)^[ \t]*(?P<kind>CHAPTER|PART)[ \t]+(?P<num>[IVXLC]+[A-Z]?)\b[ \t.:—–-]*(?P<rest>[^\n]*)$"
)
_SCHEDULE = re.compile(
    r"(?m)^[ \t]*\[?[ \t]*(?:THE[ \t]+)?"
    r"(?P<ord>FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH|ELEVENTH|TWELFTH)?"
    r"[ \t]*SCHEDULE[ \t]*\.?[ \t]*$"
)
_TOC_LINE = re.compile(r"^\s*\d{1,3}[A-Z]{0,3}\.\s+[^—–]*$")
MAX_SECTION_GAP = 60


def chunker_fingerprint(max_chars: int, overlap: int) -> str:
    return f"{CHUNKER_VERSION}-max{max_chars}-ov{overlap}"


@dataclass
class Unit:
    label: str
    number: str | None
    title: str | None
    hierarchy: str | None
    start: int
    end: int


@dataclass
class ParsedDocument:
    spec: DocumentSpec
    chunks: list[Chunk]
    units: int
    pages: int
    title: str
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _num_key(num: str) -> tuple[int, str]:
    m = re.match(r"(\d+)([A-Z]*)", num)
    assert m is not None
    return int(m.group(1)), m.group(2)


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip().rstrip(".").strip()
    return title.strip("[] ")


def _division_label(match: re.Match[str], text: str) -> str:
    kind, num, rest = match.group("kind").title(), match.group("num"), match.group("rest").strip()
    if not rest:
        nxt = text[match.end() :].lstrip("\n").split("\n", 1)[0].strip()
        letters = [c for c in nxt if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
            rest = nxt
    rest = rest.replace("[", "").replace("]", "").strip(" .:—–-")
    return f"{kind} {num}: {rest.capitalize()}" if rest else f"{kind} {num}"


# ---------------------------------------------------------------------------
# Statutes and the Constitution
# ---------------------------------------------------------------------------


def find_statute_units(text: str, unit_label: str) -> list[Unit]:
    candidates = list(_HEADING.finditer(text))
    body_start = next((m.start() for m in candidates if m.group("num") == "1"), None)
    if body_start is None:
        return []
    schedule_match = next((m for m in _SCHEDULE.finditer(text) if m.start() > body_start), None)
    schedule_start = schedule_match.start() if schedule_match else len(text)

    accepted: list[re.Match[str]] = []
    last: tuple[int, str] | None = None
    for m in candidates:
        if m.start() < body_start or m.start() >= schedule_start:
            continue
        key = _num_key(m.group("num"))
        if last is None:
            if key[0] != 1:
                continue
        elif not (key > last and key[0] - last[0] <= MAX_SECTION_GAP):
            continue
        accepted.append(m)
        last = key

    divisions = [(m.start(), m.group("kind"), _division_label(m, text)) for m in _DIVISION.finditer(text)]

    def hierarchy_at(pos: int) -> str | None:
        part = chapter = None
        for start, kind, label in divisions:
            if start < body_start - 400 or start > pos:  # ignore the table of contents
                continue
            if kind == "PART":
                part, chapter = label, None
            else:
                chapter = label
        return " › ".join(x for x in (part, chapter) if x) or None

    units: list[Unit] = []
    preamble = _preamble(text, body_start)
    if preamble:
        units.append(Unit("Preamble", None, None, None, preamble[0], preamble[1]))
    for i, m in enumerate(accepted):
        end = accepted[i + 1].start() if i + 1 < len(accepted) else schedule_start
        units.append(
            Unit(
                unit_label,
                m.group("num"),
                _clean_title(m.group("title")),
                hierarchy_at(m.start()),
                m.start(),
                end,
            )
        )
    if schedule_match:
        sched = [m for m in _SCHEDULE.finditer(text) if m.start() >= schedule_start]
        for i, m in enumerate(sched):
            end = sched[i + 1].start() if i + 1 < len(sched) else len(text)
            if len(text[m.start() : end].strip()) > 40:
                ordinal = (m.group("ord") or "").title() or None
                units.append(Unit("Schedule", ordinal, None, None, m.start(), end))
    return units


def _preamble(text: str, body_start: int) -> tuple[int, int] | None:
    """Text just before section 1 back to the end of the table of contents (at most ~3,000 chars)."""
    lines = text[:body_start].split("\n")
    kept = 0
    for line in reversed(lines):
        stripped = line.strip()
        if _TOC_LINE.match(line) or "SCHEDULE" in stripped or "APPENDIX" in stripped.upper():
            break
        kept += len(line) + 1
        if kept > 3000:
            break
    start = max(0, body_start - kept)
    return (start, body_start) if len(text[start:body_start].strip()) > 80 else None


# ---------------------------------------------------------------------------
# Judgments (JUDIS format)
# ---------------------------------------------------------------------------

_JUDIS = {
    "petitioner": re.compile(r"PETITIONER:\s*(.+?)\s*\bVs\.?\s", re.S),
    "respondent": re.compile(r"RESPONDENT:\s*(.+?)\s*DATE OF JUDGMENT", re.S),
    "date": re.compile(r"DATE OF JUDGMENT:?\s*(\d{1,2}/\d{1,2}/\d{4})"),
    "bench": re.compile(r"BENCH:\s*(.+?)\s*(?:BENCH:|CITATION:|ACT:|HEADNOTE:)", re.S),
    "citation": re.compile(r"CITATION:\s*(.+?)\s*(?:ACT:|HEADNOTE:|JUDGMENT:)", re.S),
}


def _party(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    name = name.split(",")[0].strip()
    name = name.title() if name.isupper() else name
    return name[:70].rstrip()


def parse_judgment_header(head: str) -> dict[str, str | None]:
    found = {k: (m.group(1) if (m := rx.search(head)) else None) for k, rx in _JUDIS.items()}
    pet, res = found["petitioner"], found["respondent"]
    case = f"{_party(pet)} v. {_party(res)}" if pet and res else None
    date = None
    if found["date"]:
        try:
            date = datetime.strptime(found["date"], "%d/%m/%Y").date().isoformat()
        except ValueError:
            date = None
    bench = re.sub(r"\s+", " ", found["bench"]).strip() if found["bench"] else None
    citation = re.sub(r"\s+", " ", found["citation"]).strip() if found["citation"] else None
    return {"case_name": case, "decision_date": date, "bench": bench or None, "citation": citation or None}


def find_judgment_units(text: str) -> list[Unit]:
    head_at = text.find("HEADNOTE:")
    body_at = text.find("JUDGMENT:", head_at if head_at != -1 else 0)
    units: list[Unit] = []
    if head_at != -1 and body_at != -1:
        act_at = text.find("ACT:")
        start = act_at if 0 <= act_at < head_at else head_at
        if len(text[start:body_at].strip()) > 120:
            units.append(Unit("Headnote", None, None, None, start, body_at))
    body_start = body_at + len("JUDGMENT:") if body_at != -1 else 0
    units.append(Unit("Judgment", None, None, None, body_start, len(text)))
    return units


# ---------------------------------------------------------------------------
# Building chunks
# ---------------------------------------------------------------------------


def _year(iso_date: str | None) -> int | None:
    return int(iso_date[:4]) if iso_date else None


def _chunk_id(doc_id: str, unit: Unit, part: int, text: str) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return str(uuid.uuid5(_NAMESPACE, f"{doc_id}|{unit.label}|{unit.number}|{unit.start}|{part}|{digest}"))


def _header(spec: DocumentSpec, title: str, unit: Unit, judgment: dict[str, str | None]) -> str:
    if spec.doc_type == "judgment":
        bits = [f"Supreme Court of India: {judgment.get('case_name') or title}"]
        if judgment.get("decision_date"):
            bits.append(f"decided {judgment['decision_date']}")
        if judgment.get("citation"):
            bits.append(str(judgment["citation"]))
        return " | ".join(bits) + f" | {unit.label}"
    if unit.number:
        head = f"{spec.short_name} {unit.label} {unit.number}"
        if unit.title:
            head += f" — {unit.title}"
    else:
        head = f"{spec.short_name} {unit.label}"
    parts = [head, title]
    if unit.hierarchy:
        parts.append(unit.hierarchy)
    return " | ".join(parts)


def build_chunks(
    spec: DocumentSpec,
    doc: DocumentText,
    doc_sha256: str,
    source_file: str,
    max_chars: int,
    overlap: int,
) -> ParsedDocument:
    fingerprint = chunker_fingerprint(max_chars, overlap)
    problems: list[str] = []
    warnings: list[str] = []
    judgment: dict[str, str | None] = {}
    title = spec.title

    if spec.doc_type in ("statute", "constitution", "amendment"):
        units = find_statute_units(doc.text, spec.unit_label)
        statute_units = sum(1 for u in units if u.label == spec.unit_label)
        if statute_units < 3 and spec.min_units == 0:
            warnings.append(f"only {statute_units} numbered units found; indexed as plain text")
            units = [Unit("Text", None, None, None, 0, len(doc.text))]
    elif spec.doc_type == "judgment":
        head_end = doc.page_starts[1] if doc.pages > 1 else len(doc.text)
        judgment = parse_judgment_header(doc.text[: max(head_end, 3000)])
        if judgment["case_name"]:
            title = f"{judgment['case_name']} ({judgment['decision_date'] or 'date unknown'})"
        else:
            warnings.append("could not read the case name from the JUDIS header")
        units = find_judgment_units(doc.text)
    else:
        units = [Unit("Text", None, None, None, 0, len(doc.text))]

    numbered = sum(1 for u in units if u.label == spec.unit_label)
    if spec.min_units and numbered < spec.min_units:
        problems.append(f"found {numbered} {spec.unit_label.lower()}s, expected at least {spec.min_units}")
    normalized = normalize_for_match(doc.text)
    for phrase in spec.expect:
        if normalize_for_match(phrase) not in normalized:
            problems.append(f"expected text not found: {phrase!r}")

    chunks: list[Chunk] = []
    for unit in units:
        body = doc.text[unit.start : unit.end].strip()
        if len(body) < 20:
            continue
        header = _header(spec, title, unit, judgment)
        pieces = [body] if len(body) <= max_chars else split_text(body, max_chars, overlap)
        parent_id = str(uuid.uuid5(_NAMESPACE, f"{spec.id}|{unit.label}|{unit.number}|{unit.start}"))
        search_from = 0
        for part, piece in enumerate(pieces, start=1):
            found = body.find(piece[:60], search_from)
            local = found if found != -1 else search_from
            search_from = max(search_from, local)
            page_start = doc.page_at(unit.start + local)
            page_end = doc.page_at(unit.start + local + len(piece) - 1)
            meta = ChunkMetadata(
                doc_id=spec.id,
                doc_title=title,
                short_name=spec.short_name if spec.doc_type != "judgment" else "SC",
                doc_type=spec.doc_type,
                source_file=source_file,
                unit_label=unit.label,
                unit=unit.number,
                unit_title=unit.title,
                hierarchy=unit.hierarchy,
                parent_id=parent_id,
                part=part,
                parts=len(pieces),
                page_start=page_start,
                page_end=page_end,
                year=spec.year or _year(judgment.get("decision_date")),
                in_force_from=spec.in_force_from.isoformat() if spec.in_force_from else None,
                repealed_on=spec.repealed_on.isoformat() if spec.repealed_on else None,
                replaced_by=spec.replaced_by,
                case_name=judgment.get("case_name"),
                decision_date=judgment.get("decision_date"),
                citation=judgment.get("citation"),
                sections=extract_sections(piece),
                doc_sha256=doc_sha256,
                chunker_version=fingerprint,
            )
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(spec.id, unit, part, piece),
                    text=piece,
                    embed_text=f"{header}\n{piece}",
                    metadata=meta,
                )
            )
    if not chunks:
        problems.append("no text extracted (scanned PDF without a text layer?)")
    return ParsedDocument(spec, chunks, numbered, doc.pages, title, problems, warnings)
