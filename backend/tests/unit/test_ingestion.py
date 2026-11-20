from __future__ import annotations

from pathlib import Path

import pytest

from lexai.ingestion.cli import swap_in
from lexai.ingestion.loader import split_text
from lexai.ingestion.metadata import classify_law_type, extract_sections, extract_year

DATA_DIR = Path(__file__).resolve().parents[2] / "Data"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("-0___jonew__judis__10693.pdf", "case_law"),
        ("IPC_1860.pdf", "statute"),
        ("CrPC_1973.pdf", "statute"),
        ("IT_Act_2000.pdf", "statute"),
        ("Criminal_Law_Amendment_2018.pdf", "statute"),
        ("Prisons_Act_1894.pdf", "statute"),
        ("Constitution_of_India.pdf", "statute"),
        ("MHA_CPRGR_Manual.pdf", "manual"),
        ("MHA_CyberCrime_Manual.pdf", "manual"),
        ("MHA_CyberFraud_Instructions.pdf", "manual"),
        ("State vs Sharma.pdf", "case_law"),
        ("random_notes.pdf", "other"),
    ],
)
def test_classify_law_type(filename: str, expected: str) -> None:
    assert classify_law_type(filename) == expected


@pytest.mark.skipif(not DATA_DIR.exists(), reason="corpus not present")
def test_no_corpus_file_is_classified_other() -> None:
    others = [p.name for p in DATA_DIR.glob("*.pdf") if classify_law_type(p.name) == "other"]
    assert others == []


def test_year_ignores_judis_ids() -> None:
    assert extract_year("IPC_1860.pdf") == 1860
    assert extract_year("-0___jonew__judis__1094.pdf") is None


def test_extract_sections() -> None:
    assert extract_sections("See Section 302 and Art. 19(1)(a), and Section 302 again") == [
        "Section 302",
        "Art. 19(1)(a)",
    ]


def test_split_text_respects_size_and_overlaps() -> None:
    text = " ".join(f"Sentence number {i} about the law." for i in range(200))
    chunks = split_text(text, size=300, overlap=60)
    assert len(chunks) > 5
    assert all(len(c) <= 300 for c in chunks)
    # consecutive chunks share some text
    assert any(chunks[0][-20:].split()[-1] in chunks[1] for _ in [0])


def test_split_text_rejects_bad_overlap() -> None:
    with pytest.raises(ValueError):
        split_text("abc", size=10, overlap=10)


def test_swap_in_keeps_a_backup(tmp_path: Path) -> None:
    live = tmp_path / "index"
    live.mkdir()
    (live / "old.txt").write_text("old")
    new = tmp_path / ".index.building"
    new.mkdir()
    (new / "new.txt").write_text("new")
    backup = swap_in(new, live)
    assert (live / "new.txt").exists()
    assert backup is not None and (backup / "old.txt").exists()
