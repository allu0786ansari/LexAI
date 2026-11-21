"""
Laptop-scale load test of the running stack (API + Qdrant + Ollama).

1. Retrieval: /api/v1/search at concurrency 1, 4 and 8 (no generation). Reports
   p50/p95/max latency, throughput and errors.
2. Answers: /api/v1/chat (streamed) at concurrency 1 and 2 with distinct
   questions and the answer cache off. Reports time to first token and total
   time; with one GPU, concurrent answers queue in Ollama, so this shows how
   waiting grows rather than a throughput figure.

A GPU-server load test (Locust, many users) belongs to the deployment phase.

Usage (from backend/, with the API NOT running):
    python scripts/load_test.py [--answers 4] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from smoke_phase1 import start_server, stop_server

SEARCH_QUESTIONS = [
    "What is the punishment for murder?",
    "Section 103 BNS",
    "What replaced section 420 IPC?",
    "Is anticipatory bail available under BNSS?",
    "What does Article 21 of the Constitution protect?",
    "Punishment for theft under BNS",
    "Admissibility of electronic records under BSA",
    "What is the right to privacy judgment?",
    "Section 66 IT Act hacking",
    "Can police arrest without a warrant?",
    "What is culpable homicide?",
    "Dowry death punishment",
    "What is criminal breach of trust?",
    "Who can grant bail for a non-bailable offence?",
    "What is the punishment for defamation?",
    "Article 14 equality before law",
]
ANSWER_QUESTIONS = [
    "What is the punishment for murder under BNS?",
    "What does Article 21 of the Constitution protect?",
    "What replaced section 420 IPC?",
    "When can police arrest without a warrant under BNSS?",
    "What is the punishment for theft under BNS?",
    "How are electronic records proved under BSA?",
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


def summary(name: str, latencies: list[float], errors: int, wall: float) -> dict[str, Any]:
    n = len(latencies)
    out = {
        "name": name,
        "requests": n + errors,
        "errors": errors,
        "p50_ms": round(pct(latencies, 50)),
        "p95_ms": round(pct(latencies, 95)),
        "max_ms": round(max(latencies)) if latencies else None,
        "mean_ms": round(statistics.fmean(latencies)) if latencies else None,
        "throughput_rps": round(n / wall, 2) if wall else None,
    }
    print(
        f"  {name:<28} n={out['requests']:<4} err={errors:<3} p50 {out['p50_ms']:>6} ms  "
        f"p95 {out['p95_ms']:>6} ms  max {out['max_ms']} ms  {out['throughput_rps']} req/s",
        flush=True,
    )
    return out


async def search_load(client: httpx.AsyncClient, concurrency: int, total: int) -> dict[str, Any]:
    latencies: list[float] = []
    errors = 0
    queue = [SEARCH_QUESTIONS[i % len(SEARCH_QUESTIONS)] for i in range(total)]

    async def worker() -> None:
        nonlocal errors
        while queue:
            question = queue.pop()
            start = time.perf_counter()
            try:
                r = await client.post("/api/v1/search", json={"question": question})
                r.raise_for_status()
                latencies.append((time.perf_counter() - start) * 1000)
            except httpx.HTTPError:
                errors += 1

    wall = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return summary(f"search c={concurrency}", latencies, errors, time.perf_counter() - wall)


async def one_answer(client: httpx.AsyncClient, question: str) -> tuple[float | None, float, bool]:
    start = time.perf_counter()
    first: float | None = None
    ok = False
    async with client.stream("POST", "/api/v1/chat", json={"question": question}) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line == "event: token" and first is None:
                first = (time.perf_counter() - start) * 1000
            if line == "event: done":
                ok = True
            if line == "event: error":
                ok = False
    return first, (time.perf_counter() - start) * 1000, ok


async def answer_load(client: httpx.AsyncClient, concurrency: int, questions: list[str]) -> dict[str, Any]:
    ttft: list[float] = []
    totals: list[float] = []
    errors = 0
    queue = list(questions)

    async def worker() -> None:
        nonlocal errors
        while queue:
            q = queue.pop()
            try:
                first, total, ok = await one_answer(client, q)
            except httpx.HTTPError:
                errors += 1
                continue
            if not ok:
                errors += 1
            totals.append(total)
            if first is not None:
                ttft.append(first)

    wall = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - wall
    out = summary(f"answer total c={concurrency}", totals, errors, elapsed)
    out["ttft"] = summary(f"answer first token c={concurrency}", ttft, 0, elapsed)
    return out


async def run(base_url: str, searches: int, answers: int) -> dict[str, Any]:
    results: dict[str, Any] = {"search": [], "answers": []}
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(1200, connect=5)) as client:
        await client.post("/api/v1/search", json={"question": "warm up"})
        print("\n1. Retrieval (/api/v1/search)", flush=True)
        for c in (1, 4, 8):
            results["search"].append(await search_load(client, c, searches))
        if answers:
            print("\n2. Answers (/api/v1/chat, cache off) - several minutes on a laptop", flush=True)
            n = min(answers, len(ANSWER_QUESTIONS))
            results["answers"].append(await answer_load(client, 1, ANSWER_QUESTIONS[:n]))
            results["answers"].append(await answer_load(client, 2, ANSWER_QUESTIONS[-n:]))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--searches", type=int, default=48, help="requests per search concurrency level")
    parser.add_argument("--answers", type=int, default=4, help="answers per concurrency level (0 = skip)")
    parser.add_argument("--server-log", type=Path, default=Path("var") / "load_test_server.log")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    for name in ("CHAT", "SEARCH", "SESSIONS"):
        os.environ[f"LEXAI_API_RATE_LIMIT_{name}"] = "10000/minute"
    os.environ["LEXAI_CACHE_ENABLED"] = "false"
    proc = start_server(args.port, args.server_log)
    try:
        results = asyncio.run(run(f"http://127.0.0.1:{args.port}", args.searches, args.answers))
    finally:
        stop_server(proc)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"\nresults: {args.json}")
    failed = sum(r["errors"] for r in results["search"] + results["answers"])
    print("\nSUMMARY:", "NO ERRORS" if not failed else f"{failed} FAILED REQUESTS")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
