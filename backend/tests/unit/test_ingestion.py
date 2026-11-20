from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from lexai.core.errors import ConfigurationError
from lexai.core.settings import REPO_ROOT, env_profile
from lexai.ingestion.chunkers import build_chunks, find_statute_units, parse_judgment_header
from lexai.ingestion.manifest import (
    DocumentSpec,
    LockEntry,
    Manifest,
    expand,
    judgment_id,
    load_manifest,
    read_bundled_sums,
    sha256_file,
    verify_source,
)
from lexai.ingestion.parsing import DocumentText, clean_page, normalize_for_match, split_text, strip_footnotes

BUNDLED = REPO_ROOT / "data" / "bundled"

# A small statute in India Code layout: an arrangement of sections (no dashes),
# then the body with chapters, an amendment marker, a numbered list inside a
# section, a long section, and a schedule.
STATUTE = """THE EXAMPLE CODE, 2023
ARRANGEMENT OF SECTIONS
CHAPTER I
PRELIMINARY
1. Short title.
2. Definitions.
CHAPTER II
OF OFFENCES
3. Punishment for murder.
3A. Punishment for attempt.
4. Repeal.
THE FIRST SCHEDULE
THE EXAMPLE CODE, 2023
ACT NO. 1 OF 2023
BE it enacted by Parliament as follows:—
CHAPTER I
PRELIMINARY
1. Short title.—(1) This Act may be called the Example Code, 2023.
2. Definitions.—In this Code, unless the context otherwise requires,—
1. this numbered item belongs to section 2.
(a) "court" means a court of law.
CHAPTER II
OF OFFENCES
3. Punishment for murder.—Whoever commits murder shall be punished with death or imprisonment for life.
[3A. Punishment for attempt.—Whoever attempts murder shall be punished. {long}]
4. Repeal.—The Old Code is hereby repealed.
THE FIRST SCHEDULE
Classification of offences under this Code. Murder is cognizable and non-bailable.
""".replace("{long}", "This is a long provision. " * 120)


def spec(**overrides: object) -> DocumentSpec:
    data: dict[str, object] = {
        "id": "example-2023",
        "title": "The Example Code, 2023",
        "short_name": "EC",
        "doc_type": "statute",
        "file": "statutes/example.pdf",
        "expect": ["whoever commits murder shall be punished"],
        "min_units": 4,
    }
    data.update(overrides)
    return DocumentSpec.model_validate(data)


def doc_text(text: str, pages: int = 2) -> DocumentText:
    cut = len(text) // 2
    return DocumentText(text, [0, cut] if pages == 2 else [0])


# ---------------------------------------------------------------- statute chunker


def test_statute_units_skip_the_table_of_contents_and_follow_numbering() -> None:
    units = find_statute_units(STATUTE, "Section")
    sections = [(u.number, u.title) for u in units if u.label == "Section"]
    assert sections == [
        ("1", "Short title"),
        ("2", "Definitions"),
        ("3", "Punishment for murder"),
        ("3A", "Punishment for attempt"),
        ("4", "Repeal"),
    ]
    two = next(u for u in units if u.number == "2")
    assert "this numbered item belongs to section 2" in STATUTE[two.start : two.end]


def test_statute_units_carry_chapters_preamble_and_schedules() -> None:
    units = {(u.label, u.number): u for u in find_statute_units(STATUTE, "Section")}
    assert units[("Section", "3")].hierarchy == "Chapter II: Of offences"
    assert units[("Section", "1")].hierarchy == "Chapter I: Preliminary"
    assert ("Preamble", None) in units
    assert ("Schedule", "First") in units


def test_long_sections_are_split_with_a_shared_parent() -> None:
    parsed = build_chunks(spec(), doc_text(STATUTE), "sha", "example.pdf", max_chars=800, overlap=100)
    pieces = [c for c in parsed.chunks if c.metadata.unit == "3A"]
    assert len(pieces) > 1
    assert {c.metadata.parent_id for c in pieces} == {pieces[0].metadata.parent_id}
    assert [c.metadata.part for c in pieces] == list(range(1, len(pieces) + 1))
    assert all(c.embed_text.startswith("EC Section 3A — Punishment for attempt") for c in pieces)
    assert all(len(c.text) <= 800 for c in pieces)
    assert parsed.problems == []


def test_chunk_ids_are_stable_between_runs() -> None:
    a = build_chunks(spec(), doc_text(STATUTE), "sha", "example.pdf", 2400, 200)
    b = build_chunks(spec(), doc_text(STATUTE), "sha", "example.pdf", 2400, 200)
    assert [c.chunk_id for c in a.chunks] == [c.chunk_id for c in b.chunks]


def test_a_truncated_statute_is_reported_not_indexed_silently() -> None:
    """The v0.1 IPC stopped before section 299; the expected text check catches that."""
    truncated = STATUTE[: STATUTE.index("3. Punishment for murder.—")]
    parsed = build_chunks(spec(), doc_text(truncated), "sha", "example.pdf", 2400, 200)
    assert any("expected text not found" in p for p in parsed.problems)
    assert any("expected at least 4" in p for p in parsed.problems)


def test_repeal_status_is_carried_into_metadata() -> None:
    old = spec(repealed_on="2024-07-01", replaced_by="new-code", min_units=0)
    parsed = build_chunks(old, doc_text(STATUTE), "sha", "example.pdf", 2400, 200)
    assert {c.metadata.repealed_on for c in parsed.chunks} == {"2024-07-01"}


# ---------------------------------------------------------------- judgments


JUDIS_HEAD = """PETITIONER:
SHRI TOPANDAS KUNDANMAL, SINCEDECEASED THEREAFTER BY HISHEIR
        Vs.
RESPONDENT:
THE STATE THROUGH THE LANDACQUISITION OFFICER, JAMNAGAR ETC.
DATE OF JUDGMENT25/07/1995
BENCH:
RAMASWAMY, K.
CITATION:
 1995 AIR 2396            1995 SCC  (5) 336
ACT:
HEADNOTE:
JUDGMENT:
These appeals are by certificate."""


def test_judgment_header_is_parsed() -> None:
    head = parse_judgment_header(JUDIS_HEAD)
    assert head["case_name"] == "Shri Topandas Kundanmal v. The State Through The Landacquisition Officer"
    assert head["decision_date"] == "1995-07-25"
    assert head["citation"] == "1995 AIR 2396 1995 SCC (5) 336"
    assert head["bench"] == "RAMASWAMY, K."


# ---------------------------------------------------------------- parsing


def test_footnotes_after_a_rule_are_removed() -> None:
    page = [
        "21. Protection of life.—No person shall be deprived.",
        "more text",
        "text",
        "_" * 30,
        "1. Ins. by Act 1.",
    ]
    assert strip_footnotes(page) == page[:3]


def test_footnotes_after_spaces_need_amendment_wording() -> None:
    lines = ["a", "b", "c", " " * 40, "1. Subs. by Act 25 of 2005, s. 23."]
    assert strip_footnotes(lines) == ["a", "b", "c"]
    body_item = ["a", "b", "c", " " * 40, "1. The accused shall be produced before the Magistrate."]
    assert strip_footnotes(body_item) == body_item  # a numbered body item is not a footnote


def test_clean_page_removes_markers_and_page_numbers() -> None:
    text = "2[21A. Right to education.—The State shall]\n42\nprovide 1*[India]"
    cleaned = clean_page(text)
    assert "[21A. Right to education" in cleaned
    assert "[India]" in cleaned
    assert "\n42\n" not in cleaned


def test_normalize_for_match_ignores_brackets_quotes_and_dashes() -> None:
    assert normalize_for_match("Whoever  commits [murder]—shall “be”") == "whoever commits murder shall be"


def test_split_text_respects_size_and_overlaps() -> None:
    text = " ".join(f"Sentence number {i} about the law." for i in range(200))
    chunks = split_text(text, size=300, overlap=60)
    assert len(chunks) > 5 and all(len(c) <= 300 for c in chunks)


def test_split_text_rejects_bad_overlap() -> None:
    with pytest.raises(ValueError):
        split_text("abc", size=10, overlap=10)


# ---------------------------------------------------------------- manifest


def test_repository_manifest_is_valid_and_complete() -> None:
    manifest = load_manifest(REPO_ROOT / "data" / "manifest.yaml")
    docs = expand(manifest, BUNDLED)
    ids = {d.id for d in docs}
    assert {"bns-2023", "bnss-2023", "bsa-2023", "ipc-1860", "crpc-1973", "iea-1872", "constitution"} <= ids
    assert sum(d.doc_type == "judgment" for d in docs) == 50
    ipc = next(d for d in docs if d.id == "ipc-1860")
    assert ipc.repealed_on == date(2024, 7, 1) and ipc.replaced_by == "bns-2023"


def test_every_bundled_file_matches_its_checksum() -> None:
    sums = read_bundled_sums(BUNDLED)
    for d in expand(load_manifest(REPO_ROOT / "data" / "manifest.yaml"), BUNDLED):
        if not d.downloaded:
            verify_source(d, BUNDLED, Path("unused"), sums, {})


def test_manifest_rejects_bad_entries() -> None:
    base = spec().model_dump(mode="json")
    with pytest.raises(ValueError, match="exactly one"):
        DocumentSpec.model_validate({**base, "urls": ["https://x/y.pdf"]})
    with pytest.raises(ValueError, match="duplicate"):
        Manifest.model_validate({"version": 1, "documents": [base, base]})
    with pytest.raises(ValueError, match="replaced_by"):
        Manifest.model_validate({"version": 1, "documents": [{**base, "replaced_by": "nothing"}]})


def test_judgment_ids() -> None:
    assert judgment_id("sc", Path("-0___jonew__judis__10693.pdf")) == "sc-judis-10693"


def test_downloaded_source_must_match_its_pin(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    downloaded = spec(file=None, urls=["https://example.org/a.pdf"])
    (raw / "example-2023.pdf").write_bytes(b"%PDF-1.4 content")
    with pytest.raises(ConfigurationError, match="not pinned"):
        verify_source(downloaded, tmp_path, raw, {}, {})
    good = LockEntry(url="u", sha256=sha256_file(raw / "example-2023.pdf"), bytes=16, fetched_at="t")
    verify_source(downloaded, tmp_path, raw, {}, {"example-2023": good})
    bad = good.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ConfigurationError, match="pinned checksum"):
        verify_source(downloaded, tmp_path, raw, {}, {"example-2023": bad})


# ---------------------------------------------------------------- real corpus


@pytest.mark.skipif(not (BUNDLED / "statutes" / "CrPC_1973.pdf").exists(), reason="corpus not present")
def test_real_crpc_is_parsed_into_sections() -> None:
    from lexai.ingestion.parsing import load_document

    crpc = next(
        d for d in expand(load_manifest(REPO_ROOT / "data" / "manifest.yaml"), BUNDLED) if d.id == "crpc-1973"
    )
    path = BUNDLED / str(crpc.file)
    parsed = build_chunks(crpc, load_document(path), "sha", path.name, 2400, 200)
    assert parsed.problems == []
    s154 = next(c for c in parsed.chunks if c.metadata.unit == "154")
    assert s154.metadata.unit_title == "Information in cognizable cases"
    assert s154.metadata.hierarchy and s154.metadata.hierarchy.startswith("Chapter XII")
    assert "Subs. by" not in s154.text  # footnotes stripped


@pytest.mark.skipif(
    not (BUNDLED / "statutes" / "Criminal_Law_Amendment_2018.pdf").exists(), reason="corpus not present"
)
def test_real_gazette_print_gets_sections_titled_from_marginal_notes() -> None:
    """The CLA 2018 PDF is a Gazette of India print, the same layout as the PRS/MHA copies of BNS/BNSS/BSA."""
    from lexai.ingestion.parsing import load_document

    spec = next(
        d
        for d in expand(load_manifest(REPO_ROOT / "data" / "manifest.yaml"), BUNDLED)
        if d.id == "criminal-law-amendment-2018"
    )
    path = BUNDLED / str(spec.file)
    doc = load_document(path)
    assert doc.layouts is not None  # detected as a Gazette print
    parsed = build_chunks(spec, doc, "sha", path.name, 2400, 200)
    assert parsed.problems == [] and parsed.warnings == []
    titles = {c.metadata.unit: c.metadata.unit_title for c in parsed.chunks if c.metadata.unit}
    assert len(titles) == 26 and list(titles)[:3] == ["1", "2", "3"]
    assert titles["1"] == "Short title and commencement"
    assert titles["5"] == "Insertion of new section 376AB"  # not the quoted section's own note
    assert titles["8"] == "Amendment of section 53A"  # printed out of order on its page
    assert titles["25"] == "Amendment of section 42 of Act No.32 of 2012"
    assert titles["26"] == "Repeal and savings"
    # masthead, running heads, legacy-font Hindi and margin citations are not in the text
    for noise in ("GAZETTE OF INDIA", "PUBLISHED BY AUTHORITY", "lañ", "\n45 of 1860.\n"):
        assert noise not in doc.text
    s2 = next(c for c in parsed.chunks if c.metadata.unit == "2")
    assert "Amendment of" not in s2.text and s2.text.startswith("2. In the Indian Penal Code")


def test_marginal_note_is_matched_by_height_within_tolerance() -> None:
    from lexai.ingestion.gazette import PageLayout

    layout = PageLayout(
        marks=[("7", 611.0), ("8", 533.0), ("9", 300.0)],
        notes=[(533.0, "Amendment of section 53A"), (613.0, "Amendment of section 376E"), (250.0, "far")],
    )
    used: set[int] = set()
    assert layout.title_for("7", used) == "Amendment of section 376E"
    assert layout.title_for("8", used) == "Amendment of section 53A"
    assert layout.title_for("9", used) is None  # nearest note is 50 points away
    assert layout.title_for("7", used) is None  # each mark is used once


RAW = REPO_ROOT / "data" / "raw"
GAZETTE_CODES = [
    # (document, sections, {section: title}) - the Gazette prints from PRS India
    (
        "bns-2023",
        358,
        {
            "1": "Short title, commencement and application",
            "103": "Punishment for murder",
            "144": "Exploitation of a trafficked person",
            "192": "Wantonly giving provocation with intent to cause riot-if rioting be committed; if not committed",
            "318": "Cheating",
            "358": "Repeal and savings",
        },
    ),
    (
        "bnss-2023",
        531,
        {
            "35": "When police may arrest without warrant",
            "173": "Information in cognizable cases",
            "482": "Direction for grant of bail to person apprehending arrest",
        },
    ),
    (
        "bsa-2023",
        170,
        {"1": "Short title, application and commencement", "63": "Admissibility of electronic records"},
    ),
]


@pytest.mark.parametrize(("doc_id", "sections", "titles"), GAZETTE_CODES)
def test_real_gazette_prints_of_the_new_codes(doc_id: str, sections: int, titles: dict[str, str]) -> None:
    """Runs where `fetch` has downloaded the PRS Gazette prints (skipped otherwise)."""
    from lexai.ingestion.manifest import read_lock
    from lexai.ingestion.parsing import load_document

    path = RAW / f"{doc_id}.pdf"
    pin = read_lock(REPO_ROOT / "data" / "manifest.lock.json").get(doc_id)
    if not path.exists() or pin is None or "prsindia" not in pin.url:
        pytest.skip("PRS Gazette print not downloaded")
    spec = next(
        d for d in expand(load_manifest(REPO_ROOT / "data" / "manifest.yaml"), BUNDLED) if d.id == doc_id
    )
    doc = load_document(path)
    parsed = build_chunks(spec, doc, "sha", path.name, 2400, 200)
    assert parsed.problems == [] and parsed.warnings == []
    found = {
        c.metadata.unit: c.metadata.unit_title for c in parsed.chunks if c.metadata.unit_label == "Section"
    }
    assert len(found) == sections
    for number, title in titles.items():
        assert found[number] == title
    for noise in ("GAZETTE OF INDIA", "PUBLISHED BY", "Legislative Counsel"):
        assert noise not in doc.text


def test_split_section_numbers_are_joined_and_column_is_the_mean() -> None:
    from lexai.ingestion.gazette import Fragment, _join_split_marks, _mode

    frags = [Fragment(142, 284, "144"), Fragment(156, 284, ". "), Fragment(160, 284, "(1) Whoever")]
    joined = _join_split_marks(frags)
    assert [f.text for f in joined] == ["144. ", "(1) Whoever"]
    # the bucket edge would be 140; the marks are at ~141.7, so a mark at 147 is within tolerance
    mode = _mode([141.7] * 20 + [147.0])
    assert mode is not None and abs(mode - 141.7) < 0.5


def test_env_profile_is_read_from_the_marker_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert env_profile(env).startswith("none")
    env.write_text("GOOGLE_API_KEY=x\n")
    assert env_profile(env).startswith("unnamed")
    env.write_text("# lexai-profile: laptop-8gb\nLEXAI_LLM_MODEL=x\n")
    assert env_profile(env) == "laptop-8gb"
