"""
Release smoke test, run through nginx after every deploy (deploy/deploy.sh) and in CI.

Waits for /readyz, then checks the web UI, a session and five questions from the
Constitution (present in every corpus, also the CI subset). A question passes when the
stream ends with `done`, the answer is not empty and retrieval returned passages.
`--min-cited N` also requires N answers to cite a source (used with the real 7B model;
the tiny CI model does not reliably write [n] markers).

    python scripts/smoke_release.py --base-url http://web:8080 [--wait-ready 300] [--min-cited 4]
                                    [--questions 5]

Exit code 0 = release healthy; 1 = roll back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx

QUESTIONS = [
    "What does Article 21 of the Constitution protect?",
    "What does Article 14 of the Constitution guarantee?",
    "Which freedoms does Article 19 of the Constitution give citizens?",
    "What remedy does Article 32 of the Constitution provide?",
    "What does Article 17 of the Constitution abolish?",
]


async def chat(client: httpx.AsyncClient, question: str, session_id: str) -> dict[str, Any]:
    events: dict[str, Any] = {}
    async with client.stream(
        "POST", "/api/v1/chat", json={"question": question, "session_id": session_id}
    ) as response:
        if response.status_code != 200:
            return {
                "error": {"status": response.status_code, "body": (await response.aread())[:300].decode()}
            }
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
                if fields.get("event") and fields["event"] != "token":
                    events[fields["event"]] = json.loads(fields["data"])
    return events


async def wait_ready(client: httpx.AsyncClient, timeout_s: float) -> tuple[bool, Any]:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while True:
        try:
            r = await client.get("/readyz")
            last = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            if r.status_code == 200:
                return True, last
        except httpx.HTTPError as exc:
            last = repr(exc)
        if time.monotonic() > deadline:
            return False, last
        await asyncio.sleep(3)


async def run(base_url: str, wait_s: float, min_cited: int, questions: int) -> bool:
    ok = True

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}{' -> ' + detail if detail else ''}", flush=True)

    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(900, connect=5)) as client:
        ready, detail = await wait_ready(client, wait_s)
        report("ready", ready, "" if ready else json.dumps(detail)[:600])
        if not ready:
            return False
        page = await client.get("/")
        report(
            "web UI served with CSP",
            page.status_code == 200
            and "<div id=" in page.text
            and "default-src 'self'" in page.headers.get("content-security-policy", ""),
            str(page.status_code),
        )
        metrics = await client.get("/metrics")
        report("metrics not public", metrics.status_code == 404, str(metrics.status_code))
        created = await client.post("/api/v1/sessions")
        report("session", created.status_code == 201, str(created.status_code))
        if created.status_code != 201:
            return False
        session = created.json()["session_id"]

        cited = 0
        asked = QUESTIONS[:questions]
        for question in asked:
            t0 = time.perf_counter()
            events = await chat(client, question, session)
            done = events.get("done") or {}
            passages = len(events.get("citations") or [])
            used = len(done.get("citations") or [])
            cited += used > 0
            passed = "error" not in events and bool(done.get("answer", "").strip()) and passages > 0
            report(
                f"answer: {question}",
                passed,
                f"{time.perf_counter() - t0:.0f} s, {passages} passages, {used} cited"
                + (f", error {events['error']}" if "error" in events else ""),
            )
        report(f"answers citing a source >= {min_cited}", cited >= min_cited, f"{cited}/{len(asked)}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://web:8080")
    parser.add_argument("--wait-ready", type=float, default=300, help="seconds to wait for /readyz")
    parser.add_argument("--min-cited", type=int, default=0)
    parser.add_argument("--questions", type=int, default=len(QUESTIONS), choices=range(1, len(QUESTIONS) + 1))
    args = parser.parse_args()
    healthy = asyncio.run(run(args.base_url, args.wait_ready, args.min_cited, args.questions))
    print(f"SMOKE: {'PASS' if healthy else 'FAIL'}")
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
