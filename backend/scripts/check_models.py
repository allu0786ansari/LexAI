"""
Check that the model server serves exactly what LexAI is configured to use.

Prints, for the configured settings (backend/.env):
  - whether the LLM and embedding models are served
  - the LLM's context length (num_ctx) from Ollama
  - the embedding dimension
  - a short generation test: load time, time to first token, tokens/second
  - how Ollama split the loaded models between GPU and CPU memory

Usage (from backend/): python scripts/check_models.py
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx

from lexai.clients.embedder import EmbeddingClient
from lexai.clients.llm import LLMClient
from lexai.core.settings import ENV_FILE, env_profile, get_settings
from lexai.domain.models import ChatMessage

failures: list[str] = []


def report(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        failures.append(name)


def ollama_root(base_url: str) -> str | None:
    """`http://localhost:11434/v1` -> `http://localhost:11434` (None if not an Ollama-style URL)."""
    return base_url.rstrip("/")[: -len("/v1")] if base_url.rstrip("/").endswith("/v1") else None


async def main() -> None:
    s = get_settings()
    print(f"Config:   {ENV_FILE} (profile: {env_profile()})")
    print(f"LLM:      {s.llm.model} at {s.llm.base_url}")
    print(f"Embedder: {s.embed.model} ({s.embed.dimension}-dim) at {s.embed.base_url}")
    print(f"Reranker: {'enabled, ' + s.rerank.model if s.rerank.enabled else 'disabled'}\n")

    if s.rerank.enabled:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            report(
                "reranker installed",
                False,
                "LEXAI_RERANK_ENABLED=true but sentence-transformers is not installed: "
                "set LEXAI_RERANK_ENABLED=false or `pip install -r requirements-rerank.txt`",
            )

    async with httpx.AsyncClient(timeout=30) as http:
        try:
            served = [
                m["id"] for m in (await http.get(f"{s.llm.base_url.rstrip('/')}/models")).json()["data"]
            ]
        except Exception as exc:
            report("model server reachable", False, f"{s.llm.base_url}: {exc!r}. Is Ollama running?")
            print("\nSUMMARY:", f"FAILED: {failures}")
            sys.exit(1)
        report("model server reachable", True, f"{len(served)} models served")

        def is_served(name: str) -> bool:
            return name in served or f"{name}:latest" in served

        report("LLM model served", is_served(s.llm.model), s.llm.model)
        hint = "" if is_served(s.embed.model) else f" (run: ollama pull {s.embed.model})"
        report("embedding model served", is_served(s.embed.model), s.embed.model + hint)

        root = ollama_root(s.llm.base_url)
        if root and is_served(s.llm.model):
            info = (await http.post(f"{root}/api/show", json={"model": s.llm.model})).json()
            params = info.get("parameters", "")
            ctx = next((line.split()[-1] for line in params.splitlines() if line.startswith("num_ctx")), None)
            report(
                "LLM context length pinned", ctx is not None, f"num_ctx={ctx or 'not set (Ollama default)'}"
            )

    embedder = EmbeddingClient(s.embed)
    try:
        health = await embedder.health()
        report(
            "embedding dimension matches config",
            health["dimension_ok"],
            f"model returns {health['dimension']}, LEXAI_EMBED_DIMENSION={s.embed.dimension}",
        )
    except Exception as exc:
        report("embedding call", False, repr(exc))
    finally:
        await embedder.aclose()

    llm = LLMClient(s.llm)
    print("\nGeneration test (the first call also loads the model into memory) ...", flush=True)
    t0 = time.perf_counter()
    first: float | None = None
    pieces: list[str] = []
    try:
        async for delta in llm.stream_chat(
            [
                ChatMessage(
                    role="user", content="In two sentences, what is a First Information Report in India?"
                )
            ],
            max_tokens=80,
        ):
            if first is None:
                first = time.perf_counter() - t0
            pieces.append(delta)
        total = time.perf_counter() - t0
        rate = len(pieces) / (total - first) if first is not None and total > first else 0.0
        report(
            "LLM generates",
            bool(pieces),
            f"first token after {first or 0:.1f}s, {len(pieces)} chunks in {total:.1f}s "
            f"(~{rate:.1f} tokens/s)",
        )
        print("   reply:", "".join(pieces).strip()[:300])
    except Exception as exc:
        report("LLM generates", False, repr(exc))
    finally:
        await llm.aclose()

    root = ollama_root(s.llm.base_url)
    if root:
        async with httpx.AsyncClient(timeout=10) as http:
            try:
                for m in (await http.get(f"{root}/api/ps")).json().get("models", []):
                    size, vram = m.get("size", 0), m.get("size_vram", 0)
                    gpu = 100 * vram / size if size else 0
                    print(f"   loaded: {m.get('name')}  {size / 1e9:.1f} GB total, {gpu:.0f}% on GPU")
            except Exception as exc:
                print("   could not read /api/ps:", repr(exc))

    print("\nSUMMARY:", "ALL PASSED" if not failures else f"FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
