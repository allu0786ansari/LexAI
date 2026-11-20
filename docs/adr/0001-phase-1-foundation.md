# ADR 0001 — Phase 1 foundation decisions

Date: 2026-11-20 · Status: proposed (pending Phase 1 run review)

## Context

The v0.1 backend blocked the event loop on every model call, silently replaced
failed embeddings with meaningless hash vectors, and depended on LangChain for
little more than a `Document` class. Phase 1 fixes the foundation without yet
changing the retrieval design (that is Phase 2–3).

## Decisions

1. **Generator: Qwen2.5-7B-Instruct.** Served as `qwen2.5-7b-instruct` by both
   Ollama (Q4_K_M, `num_ctx 8192` pinned in `deploy/ollama/Modelfile`) and vLLM
   (AWQ, `--served-model-name qwen2.5-7b-instruct`), so config is identical.
   Sampling parameters are always sent explicitly.
2. **Own OpenAI-compatible client on `httpx`, not the `openai` SDK.** We use two
   endpoints. The SDK's v3 moved to a separate HTTP stack (`httpx2`), which our
   test tooling (`respx`) cannot mock; a ~150-line client keeps streaming,
   retries and timeouts explicit. Retries happen only before the first token.
3. **LangChain removed.** Replaced by Pydantic models (`Chunk`, `ChatMessage`,
   `Citation`) and plain code.
4. **No embedding fallback.** Embedding failures raise `UpstreamUnavailable`.
5. **Interim index without pickle.** FAISS's own file format + `chunks.jsonl`
   + `index_meta.json` (embedding model, dimension, schema version). The API
   refuses an index built by a different model. Replaced by Qdrant in Phase 2.
6. **Embeddings: `qwen3-embedding:0.6b` (1024-dim)** with a query instruction;
   documents are embedded without one. Subject to the Phase 4 A/B against bge-m3.
7. **Reranker stays in-process for now**, run in a worker thread, optional
   install extra. Moves to a TEI container in Phase 6.
8. **HyDE off by default.** It adds a full 7B call per question; Phase 4 decides.
9. **Interim chunking:** a simple recursive splitter (1,500 chars, 200 overlap)
   replaces the semantic chunker; Phase 2 adds legal-structure chunking.
10. **Filename rules fixed** so no corpus file is labelled `other`; Phase 2
    replaces them with `data/manifest.yaml`.

## Consequences

- The existing index must be rebuilt (`lexai-ingest`); old pickle indexes are not read.
- API paths are unchanged, so the v0.1 frontend keeps working until Phase 5.
- Evaluation scripts were removed (their scores were not trustworthy); Phase 4 rebuilds them.
