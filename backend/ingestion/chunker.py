"""
Semantic chunking for legal documents.

Deliberately NOT implemented via `langchain_experimental.text_splitter.
SemanticChunker`: as of this writing that package is officially sunset and
no longer actively maintained (see
https://github.com/langchain-ai/langchain-experimental/issues/87). Taking a
dependency on an unmaintained package for a core pipeline component is a
liability we don't need — the underlying algorithm (embed sentences, split
where consecutive-sentence semantic distance spikes) is simple enough to
own directly, with a project-appropriate safety net for oversized chunks.

Algorithm:
  1. Split page text into sentences.
  2. Combine each sentence with its neighbours (buffer_size window) so the
     embedding captures local context rather than a single short sentence.
  3. Embed every combined-window text in one batch call.
  4. Compute cosine distance between each consecutive pair of embeddings.
  5. Anything above the configured percentile of those distances is a
     chunk boundary.
  6. Merge any resulting chunk under `min_chunk_chars` into its neighbour.
  7. Force-split any chunk over `max_chunk_chars` with a plain recursive
     character splitter — legal statutes can have very long unbroken
     clauses, and an unbounded chunk would blow the LLM context budget.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT_REGEX = re.compile(r"(?<=[.?!])\s+(?=[A-Z(\"'\u201c])")


@dataclass
class SemanticChunkerConfig:
    buffer_size: int = 1
    breakpoint_percentile: float = 92.0
    min_chunk_chars: int = 250
    max_chunk_chars: int = 2200
    hard_split_overlap: int = 150


class SemanticChunker:
    """Splits text at points of maximal semantic discontinuity between sentences."""

    def __init__(self, embeddings: Embeddings, config: SemanticChunkerConfig | None = None):
        self.embeddings = embeddings
        self.config = config or SemanticChunkerConfig()
        self._safety_net = RecursiveCharacterTextSplitter(
            chunk_size=self.config.max_chunk_chars,
            chunk_overlap=self.config.hard_split_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

    def split_text(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []
        sentences = self._split_sentences(text)
        if len(sentences) <= 1:
            return self._apply_size_safety_net(sentences or [text])

        windows = self._combine_with_buffer(sentences)
        embeddings = self.embeddings.embed_documents(windows)
        distances = self._consecutive_distances(embeddings)

        if not distances:
            return self._apply_size_safety_net(["".join(sentences)])

        threshold = float(np.percentile(distances, self.config.breakpoint_percentile))
        breakpoints = {i for i, d in enumerate(distances) if d > threshold}

        raw_chunks: list[str] = []
        current: list[str] = []
        for i, sentence in enumerate(sentences):
            current.append(sentence)
            if i in breakpoints:
                raw_chunks.append("".join(current))
                current = []
        if current:
            raw_chunks.append("".join(current))

        merged = self._merge_small_chunks(raw_chunks)
        return self._apply_size_safety_net(merged)

    def split_documents(self, documents: list[Document]) -> list[Document]:
        """
        Split a list of page-level Documents into semantically-chunked
        Documents, preserving and extending each source Document's metadata.
        """
        output: list[Document] = []
        for doc in documents:
            pieces = self.split_text(doc.page_content)
            for piece in pieces:
                if not piece.strip():
                    continue
                output.append(Document(page_content=piece.strip(), metadata=dict(doc.metadata)))
        return output

    # -- internals ---------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        raw = _SENTENCE_SPLIT_REGEX.split(text)
        # Re-attach the trailing whitespace/newline structure is not needed;
        # normalise each sentence to end with a single trailing space so
        # `"".join(...)` reconstructs readable text.
        return [s.strip() + " " for s in raw if s.strip()]

    def _combine_with_buffer(self, sentences: list[str]) -> list[str]:
        b = self.config.buffer_size
        n = len(sentences)
        windows = []
        for i in range(n):
            lo, hi = max(0, i - b), min(n, i + b + 1)
            windows.append("".join(sentences[lo:hi]))
        return windows

    @staticmethod
    def _consecutive_distances(embeddings: list[list[float]]) -> list[float]:
        vecs = np.array(embeddings, dtype=np.float32)
        if len(vecs) < 2:
            return []
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        unit = vecs / norms
        # Cosine distance between consecutive rows: 1 - cos_sim
        sims = np.sum(unit[:-1] * unit[1:], axis=1)
        return (1.0 - sims).tolist()

    def _merge_small_chunks(self, chunks: list[str]) -> list[str]:
        if not chunks:
            return chunks
        merged: list[str] = []
        buffer = ""
        for chunk in chunks:
            buffer = buffer + chunk if buffer else chunk
            if len(buffer) >= self.config.min_chunk_chars:
                merged.append(buffer)
                buffer = ""
        if buffer:
            if merged:
                merged[-1] = merged[-1] + buffer
            else:
                merged.append(buffer)
        return merged

    def _apply_size_safety_net(self, chunks: list[str]) -> list[str]:
        final: list[str] = []
        for chunk in chunks:
            if len(chunk) <= self.config.max_chunk_chars:
                final.append(chunk)
            else:
                logger.debug(
                    "Chunk of %d chars exceeds max_chunk_chars=%d, force-splitting.",
                    len(chunk), self.config.max_chunk_chars,
                )
                final.extend(self._safety_net.split_text(chunk))
        return final
