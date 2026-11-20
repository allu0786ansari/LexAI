"""PDF loading and interim text splitting (Phase 2 replaces splitting with legal-structure chunking)."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from lexai.core.logging import get_logger
from lexai.domain.models import Chunk, ChunkMetadata
from lexai.ingestion.metadata import classify_law_type, extract_sections, extract_year

logger = get_logger(__name__)
# pypdf logs a warning for every odd font or object it skips; keep only real errors.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MIN_PAGE_CHARS = 30
_SEPARATORS = ["\n\n", "\n", ". ", " "]


@dataclass
class Page:
    number: int
    text: str


def load_pdf_pages(path: Path) -> list[Page]:
    """One entry per page with extractable text; pages with almost no text (blank or scanned) are skipped."""
    try:
        reader = PdfReader(str(path))
    except (PdfReadError, OSError) as exc:
        logger.error("pdf_unreadable", file=path.name, error=str(exc))
        return []
    pages: list[Page] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pypdf raises many parser-specific errors on malformed pages
            logger.warning("page_extract_failed", file=path.name, page=number, error=str(exc))
            continue
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) >= MIN_PAGE_CHARS:
            pages.append(Page(number, text))
    return pages


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """
    Recursive character splitter: split on the coarsest separator that keeps
    pieces under `size`, then merge neighbours back up to `size` with `overlap`.
    """
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    pieces = _split_recursive(text.strip(), size, 0)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > size:
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            # start the overlap at a word boundary
            space = tail.find(" ")
            current = (tail[space + 1 :] if space != -1 else tail) + piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def _split_recursive(text: str, size: int, level: int) -> list[str]:
    if len(text) <= size:
        return [text]
    if level >= len(_SEPARATORS):
        return [text[i : i + size] for i in range(0, len(text), size)]
    sep = _SEPARATORS[level]
    parts = text.split(sep)
    out: list[str] = []
    for i, part in enumerate(parts):
        piece = part + (sep if i < len(parts) - 1 else "")
        if len(piece) <= size:
            out.append(piece)
        else:
            out.extend(_split_recursive(piece, size, level + 1))
    return out


def chunk_document(path: Path, size: int, overlap: int) -> tuple[list[Chunk], int]:
    """Return the chunks of one PDF and its number of text pages."""
    pages = load_pdf_pages(path)
    law_type = classify_law_type(path.name)
    year = extract_year(path.name)
    chunks: list[Chunk] = []
    for page in pages:
        for text in split_text(page.text, size, overlap):
            index = len(chunks)
            digest = hashlib.sha256(f"{path.name}:{page.number}:{index}:{text}".encode()).hexdigest()[:16]
            chunks.append(
                Chunk(
                    chunk_id=digest,
                    text=text,
                    metadata=ChunkMetadata(
                        source_file=path.name,
                        law_type=law_type,
                        year=year,
                        page=page.number,
                        chunk_index=index,
                        sections=extract_sections(text),
                    ),
                )
            )
    return chunks, len(pages)
