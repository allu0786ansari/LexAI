"""
Phase 1 smoke test against a running API (real Ollama, real index).

Checks the Phase 1 exit criteria that unit tests cannot:
  1. /readyz is green with the real models and index.
  2. Tokens really stream: the first token arrives well before the last one.
  3. /healthz stays fast while several answers stream at once (no event-loop blocking).
  4. A client that disconnects mid-answer does not break the server.

With --expect-down (run after stopping Ollama) it instead checks that
/readyz is red and a question returns an `error` event, not an answer.

Usage (from backend/, with the virtual environment's python):
    python scripts/smoke_phase1.py --start-server            # starts and stops the API itself
    python scripts/smoke_phase1.py --base-url http://localhost:8000   # against a running API
    python scripts/smoke_phase1.py --start-server --expect-down
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
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
    async with client.stream("POST", "/api/v1/chat", json={"question": question}) as response:
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
            cites = first["done"]["citations"]
            print("citations:")
            for c in cites:
                status = f"  [{c['status']}]" if c.get("status") else ""
                title = f" - {c['unit_title']}" if c.get("unit_title") else ""
                print(f"  [{c['ref']}] {c.get('label')}{title} (p.{c['page']}){status}")
            print("\nANSWER:\n" + first["done"]["answer"] + "\n")
            record("answer has citations", len(cites) > 0, f"{len(cites)}")
            labels = [c.get("label") or "" for c in cites]
            record(
                "citations name the provision",
                bool(cites) and all(labels),
                ", ".join(labels) or "none",
            )
            murder = [c for c in cites if c.get("label") in ("IPC s.302", "BNS s.103")]
            record(
                "murder question cites IPC s.302 or BNS s.103",
                bool(murder),
                ", ".join(c["label"] for c in murder) or "neither was retrieved",
            )
            ipc = next((c for c in cites if (c.get("label") or "").startswith("IPC ")), None)
            if ipc is not None:
                record(
                    "repealed IPC is flagged in its citation",
                    "repealed" in (ipc.get("status") or "").lower(),
                    repr(ipc.get("status")),
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
        # Blocking (the v0.1 bug) shows up as seconds; 500 ms leaves room for a laptop whose
        # CPU is saturated by the model server.
        record(
            "/healthz responsive during generation",
            p95 < 500,
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


BACKEND_DIR = Path(__file__).resolve().parents[1]


def start_server(port: int, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "wb")  # noqa: SIM115 - closed when the process is stopped
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "lexai.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=BACKEND_DIR,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    print(f"API starting (pid {proc.pid}), log: {log_path}", flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"API exited during start-up (code {proc.returncode}); see {log_path}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200:
                print("API is up.", flush=True)
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(1)
    proc.kill()
    raise SystemExit(f"API did not start within 15 minutes; see {log_path}")


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("API stopped.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None, help="Use an already running API")
    parser.add_argument(
        "--start-server", action="store_true", help="Start the API for the test, stop it after"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--server-log", type=Path, default=BACKEND_DIR / "var" / "smoke_server.log")
    parser.add_argument("--expect-down", action="store_true")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        choices=range(1, 6),
        help="Parallel questions (2 suits a laptop)",
    )
    args = parser.parse_args()
    base_url = args.base_url or f"http://127.0.0.1:{args.port}"
    proc = start_server(args.port, args.server_log) if args.start_server else None
    try:
        asyncio.run(check_down(base_url) if args.expect_down else check_up(base_url, args.concurrency))
    finally:
        if proc is not None:
            stop_server(proc)
    failed = [name for name, ok, _ in results if not ok]
    print("\nSUMMARY:", "ALL PASSED" if not failed else f"FAILED: {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
