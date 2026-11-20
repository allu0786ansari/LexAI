"""
Phase 1 smoke test against a running API (real Ollama, real index).

Checks the Phase 1 exit criteria that unit tests cannot:
  1. /readyz is green with the real models and index.
  2. Tokens really stream: the first token arrives well before the last one.
  3. /healthz stays fast while several answers stream at once (no event-loop blocking).
  4. A client that disconnects mid-answer does not break the server.

With --expect-down (run after stopping Ollama) it instead checks that
/readyz is red and a question returns an `error` event, not an answer.

Usage:
    uv run python scripts/smoke_phase1.py [--base-url http://localhost:8000]
    uv run python scripts/smoke_phase1.py --expect-down
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from typing import Any

import httpx

QUESTION = "What is the punishment for murder under the Indian Penal Code?"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


async def stream_question(
    client: httpx.AsyncClient, question: str, stop_after_first: bool = False
) -> dict[str, Any]:
    t0 = time.perf_counter()
    token_times: list[float] = []
    events: list[str] = []
    done: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    async with client.stream("POST", "/api/query", json={"question": question}) as response:
        response.raise_for_status()
        event = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
                events.append(event)
            elif line.startswith("data:") and event:
                data = json.loads(line[5:].strip())
                if event == "token":
                    token_times.append(time.perf_counter() - t0)
                    if stop_after_first:
                        break
                elif event == "done":
                    done = data
                elif event == "error":
                    error = data
    return {
        "events": events,
        "token_times": token_times,
        "done": done,
        "error": error,
        "total_s": time.perf_counter() - t0,
    }


async def check_up(base_url: str, concurrency: int) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(600, connect=5)) as client:
        ready = await client.get("/readyz")
        body = ready.json()
        print("readyz:", json.dumps(body, indent=2))
        record("readyz green", ready.status_code == 200, f"HTTP {ready.status_code}")

        print(f"\nAsking: {QUESTION}")
        first = await stream_question(client, QUESTION)
        tt = first["token_times"]
        if first["error"] or not tt:
            record("answer streamed", False, f"error={first['error']} events={first['events'][:5]}")
        else:
            spread = tt[-1] - tt[0]
            record(
                "tokens stream incrementally",
                len(tt) > 5 and spread > 0.2,
                f"{len(tt)} token events; first at {tt[0]:.2f}s, "
                f"last at {tt[-1]:.2f}s (spread {spread:.2f}s)",
            )
            assert first["done"] is not None
            lat = first["done"]["latency"]
            print("latency breakdown (ms):", json.dumps(lat))
            print("citations:", [(c["ref"], c["source_file"], c["page"]) for c in first["done"]["citations"]])
            print("\nANSWER:\n" + first["done"]["answer"] + "\n")
            record(
                "answer has citations",
                len(first["done"]["citations"]) > 0,
                f"{len(first['done']['citations'])}",
            )

        print(f"Running {concurrency} concurrent questions while polling /healthz ...")
        health_ms: list[float] = []
        stop = asyncio.Event()

        async def poll() -> None:
            while not stop.is_set():
                t = time.perf_counter()
                await client.get("/healthz")
                health_ms.append((time.perf_counter() - t) * 1000)
                await asyncio.sleep(0.1)

        poller = asyncio.create_task(poll())
        questions = [
            "What does Article 21 of the Constitution protect?",
            "What is the punishment for cheating?",
            "What is hacking under the IT Act?",
            "When can police arrest without a warrant?",
            "What is culpable homicide?",
        ][:concurrency]
        runs = await asyncio.gather(*(stream_question(client, q) for q in questions))
        stop.set()
        await poller
        ok_runs = sum(1 for r in runs if r["done"])
        p95 = statistics.quantiles(health_ms, n=20)[-1] if len(health_ms) >= 20 else max(health_ms or [0])
        record(
            f"{len(questions)} concurrent answers completed",
            ok_runs == len(questions),
            f"{ok_runs}/{len(questions)} finished",
        )
        record(
            "/healthz responsive during generation",
            p95 < 100,
            f"{len(health_ms)} probes, p50 {statistics.median(health_ms):.1f} ms, p95 {p95:.1f} ms",
        )

        print("Disconnecting a client after its first token ...")
        await stream_question(client, QUESTION, stop_after_first=True)
        await asyncio.sleep(1)
        after = await client.get("/healthz")
        record(
            "server healthy after client disconnect", after.status_code == 200, f"HTTP {after.status_code}"
        )


async def check_down(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        ready = await client.get("/readyz")
        print("readyz:", json.dumps(ready.json(), indent=2))
        record("readyz red when Ollama is down", ready.status_code == 503, f"HTTP {ready.status_code}")
        run = await stream_question(client, QUESTION)
        err = run["error"] or {}
        record(
            "question returns an error event, not an answer",
            bool(err) and not run["token_times"],
            f"error={err}",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--expect-down", action="store_true")
    parser.add_argument(
        "--concurrency", type=int, default=5, choices=range(1, 6), help="Use 2 on CPU-only machines"
    )
    args = parser.parse_args()
    asyncio.run(check_down(args.base_url) if args.expect_down else check_up(args.base_url, args.concurrency))
    failed = [name for name, ok, _ in results if not ok]
    print("\nSUMMARY:", "ALL PASSED" if not failed else f"FAILED: {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
