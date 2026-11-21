"""
Phase 5 verification against the real index and models (starts its own API).

B1. Sessions: a forged session id is refused (404); a server-issued one works.
B2. Security: oversized body -> 413; security headers; admin without/with a wrong key -> 401.
B3. Sources: the corpus list; for three real answers, every citation opens its passage
    (GET /chunks/{id}) and its PDF page (GET pdf_url).
B4. Feedback is stored; /metrics shows answers, time to first token and stage latency.
B5. Tracing: the follow-up's trace has one span per stage (condense, retrieve, context,
    generate, verify) with times, read from the local trace file.
B6. Admin reindex of one small document: the job succeeds, every other document is
    kept, and the API answers from the new index without a restart. The API's memory is
    sampled meanwhile; if the API dies, its exit code and log tail are shown, it is
    restarted, the live index is checked intact, and the reindex is retried once.
B7. Rate limit: of 21 chat requests sent at once, 20 are answered and one gets
    429 with Retry-After (refused questions: no model call).

Usage (from backend/, with the API NOT running): python scripts/check_phase5.py
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from show_trace import tree
from smoke_phase1 import describe_exit, start_server, stop_server

failures: list[str] = []
FORGED = "2b1f7c9e-4d3a-4f6b-9e8d-1a2b3c4d5e6f"
REFUSED = "How do I destroy the CCTV evidence before the police come?"


def report(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def read_spans(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def chat(client: httpx.AsyncClient, question: str, session_id: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"question": question}
    if session_id:
        body["session_id"] = session_id
    events: dict[str, Any] = {"_status": None}
    async with client.stream("POST", "/api/v1/chat", json=body) as response:
        events["_status"] = response.status_code
        if response.status_code != 200:
            events["_body"] = json.loads(await response.aread())
            events["_headers"] = dict(response.headers)
            return events
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
                if "event" in lines:
                    events[lines["event"]] = json.loads(lines["data"])
    return events


class Server:
    """The API under test; restartable if it dies."""

    def __init__(self, port: int, log: Path) -> None:
        self.port, self.log, self.restarts = port, log, 0
        self.proc = start_server(port, log)

    def restart(self) -> None:
        self.restarts += 1
        self.log = self.log.with_name(f"{self.log.stem}_restart{self.restarts}{self.log.suffix}")
        print(f"   restarting the API (log: {self.log})", flush=True)
        self.proc = start_server(self.port, self.log)

    def stop(self) -> None:
        stop_server(self.proc)


def memory_mb(pid: int) -> int | None:
    """Resident memory of a process, without extra packages (tasklist on Windows, /proc on Linux)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            fields = next(csv.reader(io.StringIO(out.strip())), [])
            digits = re.sub(r"\D", "", fields[-1]) if fields else ""
            return int(digits) // 1024 if digits else None
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


async def reindex(
    client: httpx.AsyncClient, server: Server, key: str, doc_id: str, chunks: int
) -> dict[str, Any]:
    """Start a reindex of one document and wait for it, sampling the API's memory; survives an API crash."""
    headers = {"X-API-Key": key}
    idle = memory_mb(server.proc.pid)
    peak = idle or 0
    t0 = time.perf_counter()
    job: dict[str, Any] = {"status": "failed", "detail": ""}
    try:
        start = await client.post("/api/v1/admin/reindex", headers=headers, json={"only": [doc_id]})
        job = start.json()
        print(
            f"   reindexing {doc_id} ({chunks} chunks): job {job.get('job_id')}, API memory {idle} MB",
            flush=True,
        )
        while (
            start.status_code == 202
            and job["status"] in ("queued", "running")
            and time.perf_counter() - t0 < 1200
        ):
            await asyncio.sleep(3)
            peak = max(peak, memory_mb(server.proc.pid) or 0)
            job = (await client.get(f"/api/v1/admin/reindex/{job['job_id']}", headers=headers)).json()
    except httpx.TransportError:
        await asyncio.sleep(2)
        return {
            "crashed": True,
            "exit": server.proc.poll(),
            "peak_mb": peak,
            "idle_mb": idle,
            "status": "crashed",
            "detail": "",
            "seconds": time.perf_counter() - t0,
        }
    return {
        "crashed": False,
        "exit": None,
        "peak_mb": peak,
        "idle_mb": idle,
        "status": job.get("status"),
        "detail": job.get("detail", ""),
        "seconds": time.perf_counter() - t0,
    }


async def check(base_url: str, admin_key: str, trace_file: Path, server: Server) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(900, connect=5)) as client:
        print("\nB1. Sessions")
        forged = await chat(client, "What is murder?", FORGED)
        report(
            "forged session refused",
            forged["_status"] == 404 and forged["_body"]["code"] == "session_not_found",
            f"{forged['_status']} {forged.get('_body', {}).get('code')}",
        )
        created = await client.post("/api/v1/sessions")
        session = created.json()["session_id"]
        report(
            "server-issued session",
            created.status_code == 201,
            f"expires in {created.json()['expires_in_s']} s",
        )

        print("\nB2. Security controls")
        big = await client.post(
            "/api/v1/chat",
            content=b'{"question":"' + b"x" * 20_000 + b'"}',
            headers={"Content-Type": "application/json"},
        )
        report("20 KB body refused", big.status_code == 413, f"{big.status_code} {big.json().get('code')}")
        h = (await client.get("/api/v1/documents")).headers
        want = {
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "x-frame-options": "DENY",
        }
        report(
            "security headers", all(h.get(k) == v for k, v in want.items()) and "content-security-policy" in h
        )
        no_key = await client.post("/api/v1/admin/reindex")
        wrong = await client.post("/api/v1/admin/reindex", headers={"X-API-Key": "wrong"})
        report(
            "admin needs the key",
            no_key.status_code == 401 and wrong.status_code == 401,
            f"{no_key.status_code}/{wrong.status_code}",
        )

        print("\nB3. Sources: every citation opens its passage and PDF page")
        docs = (await client.get("/api/v1/documents")).json()
        with_pdf = sum(d["has_pdf"] for d in docs)
        report("corpus list", len(docs) > 10, f"{len(docs)} documents, {with_pdf} with a PDF on disk")
        questions = [
            ("What is the punishment for murder?", session),
            ("Is it bailable?", session),
            ("What does Article 21 of the Constitution protect?", None),
        ]
        answers = []
        opened = total = pdf_ok = pdf_total = 0
        for q, sid in questions:
            t0 = time.perf_counter()
            out = await chat(client, q, sid)
            answers.append(out)
            done = out.get("done") or {}
            print(
                f"   {q!r}: {len(done.get('citations', []))} citations, {time.perf_counter() - t0:.0f} s, "
                f"verified={(done.get('verification') or {}).get('valid')}"
            )
            for c in done.get("citations", []):
                total += 1
                r = await client.get(f"/api/v1/chunks/{c['chunk_id']}")
                if r.status_code == 200 and r.json()["label"] == c["label"] and r.json()["text"].strip():
                    opened += 1
                else:
                    print(f"      cannot open {c['label']} ({c['chunk_id']}): {r.status_code}")
                    continue
                pdf_url = r.json()["pdf_url"]
                if pdf_url:
                    pdf_total += 1
                    p = await client.get(pdf_url.split("#")[0])
                    # Readers accept the header anywhere in the first 1 KB (some court PDFs have stray bytes).
                    if p.status_code == 200 and b"%PDF" in p.content[:1024]:
                        pdf_ok += 1
                    else:
                        print(f"      PDF for {c['label']} failed: {p.status_code}")
        report("every citation opens its passage", total > 0 and opened == total, f"{opened}/{total}")
        report(
            "every PDF link opens",
            pdf_ok == pdf_total,
            f"{pdf_ok}/{pdf_total} (citations whose source PDF is on disk)",
        )

        print("\nB4. Feedback and metrics")
        rid = (answers[0].get("done") or {}).get("request_id", "missing-request")
        fb = await client.post(
            "/api/v1/feedback", json={"request_id": rid, "rating": "up", "comment": "phase 5 check"}
        )
        report("feedback stored", fb.status_code == 201, f"id {fb.json().get('id')}")
        metrics = (await client.get("/metrics")).text

        def value(prefix: str) -> float:
            return sum(
                float(line.rsplit(" ", 1)[1]) for line in metrics.splitlines() if line.startswith(prefix)
            )

        answered = value('lexai_answers_total{outcome="answered"}') + value(
            'lexai_answers_total{outcome="cached"}'
        )
        report("answers counted", answered >= 3, f"{answered:.0f}")
        report("time to first token recorded", value("lexai_time_to_first_token_seconds_count") >= 3)
        report("stage latency recorded", value('lexai_stage_duration_seconds_count{stage="retrieve"}') >= 3)
        report("route templates, not paths", 'route="/api/v1/chunks/{chunk_id}"' in metrics)

        print("\nB5. One question traced from request to verified answer")
        trace_id = (answers[1].get("meta") or {}).get("trace_id", "")
        spans: list[dict[str, Any]] = []
        for _ in range(20):  # spans are exported in batches every few seconds
            await asyncio.sleep(1)
            spans = [s for s in read_spans(trace_file) if s["trace_id"] == trace_id]
            if any(s["name"] == "lexai.verify" for s in spans):
                break
        names = {s["name"] for s in spans}
        stages = ["lexai.condense", "lexai.retrieve", "lexai.context", "lexai.generate", "lexai.verify"]
        report(
            "trace has every stage",
            bool(trace_id) and set(stages) <= names,
            f"trace {trace_id[:16]}…, {len(spans)} spans",
        )
        print("\n".join("   " + line for line in tree(spans)))

        print("\nB6. Admin reindex (one document; the others are carried over)")
        small = min(
            (d for d in docs if d["doc_type"] == "statute" and d["chunks"] > 0), key=lambda d: d["chunks"]
        )
        before = (answers[2].get("done") or {}).get("index_version", "")
        chunks_before = sum(d["chunks"] for d in docs)
        result = await reindex(client, server, admin_key, small["doc_id"], small["chunks"])
        if result["crashed"]:
            report(
                "reindex job succeeded",
                False,
                f"the API process died during the reindex: {describe_exit(result['exit'])}; "
                f"API memory peaked at {result['peak_mb']} MB",
            )
            print("   last lines of the server log:")
            print("\n".join("      " + line for line in tail(server.log, 25)))
            server.restart()
            docs_now = (await client.get("/api/v1/documents")).json()
            chunks_now = sum(d["chunks"] for d in docs_now)
            report(
                "index intact after the crash",
                chunks_now == chunks_before,
                f"{chunks_now}/{chunks_before} chunks",
            )
            survived = await chat(client, "What does Article 21 of the Constitution protect?", None)
            report(
                "API answers after a restart",
                "done" in survived,
                f"index {(survived.get('done') or {}).get('index_version')}",
            )
            print("   retrying the reindex once (it also removes the interrupted build)")
            result = await reindex(client, server, admin_key, small["doc_id"], small["chunks"])
            if result["crashed"]:
                report("reindex succeeded on retry", False, f"died again: {describe_exit(result['exit'])}")
                server.restart()
        report(
            "reindex job succeeded" if server.restarts == 0 else "reindex succeeded on retry",
            result["status"] == "succeeded",
            f"{result['detail'][:120]} ({result['seconds']:.0f} s; "
            f"API memory {result['idle_mb']} -> peak {result['peak_mb']} MB)",
        )
        docs_after = (await client.get("/api/v1/documents")).json()
        chunks_after = sum(d["chunks"] for d in docs_after)
        report("no document lost", chunks_after == chunks_before, f"{chunks_after}/{chunks_before} chunks")
        again = await chat(client, "What does Article 21 of the Constitution protect?", None)
        after = (again.get("done") or {}).get("index_version", "")
        report("answers come from the new index", bool(after) and after != before, f"{before} -> {after}")

        print("\nB7. Rate limit (21 refused questions at once: refused without a model call)")
        await asyncio.sleep(61)  # let earlier chat requests leave the one-minute window
        limit_session = (await client.post("/api/v1/sessions")).json()["session_id"]
        t0 = time.perf_counter()
        outs = await asyncio.gather(*(chat(client, REFUSED, limit_session) for _ in range(21)))
        codes = sorted(o["_status"] for o in outs)
        retry_after = next((o["_headers"].get("retry-after") for o in outs if o["_status"] == 429), None)
        report(
            "20 allowed, the 21st refused",
            codes.count(200) == 20 and codes.count(429) == 1 and retry_after is not None,
            f"{codes.count(200)} x 200, {codes.count(429)} x 429 in {time.perf_counter() - t0:.1f} s; "
            f"Retry-After {retry_after}",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--server-log", type=Path, default=Path("var") / "check_phase5_server.log")
    args = parser.parse_args()
    admin_key = secrets.token_urlsafe(24)
    trace_file = Path("var") / "check_phase5_traces.jsonl"
    trace_file.unlink(missing_ok=True)
    os.environ["LEXAI_API_ADMIN_API_KEY"] = admin_key
    os.environ["LEXAI_OBS_TRACE_FILE"] = str(trace_file.resolve())
    os.environ["LEXAI_API_ENVIRONMENT"] = "local"
    server = Server(args.port, args.server_log)
    try:
        asyncio.run(check(f"http://127.0.0.1:{args.port}", admin_key, trace_file, server))
    except httpx.TransportError as exc:  # the API died outside the reindex step
        report("API stayed up", False, f"{exc!r}; {describe_exit(server.proc.poll())}")
        print("\n".join("      " + line for line in tail(server.log, 25)))
    finally:
        server.stop()
    print("\nSUMMARY:", "ALL PASSED" if not failures else f"FAILED ({len(failures)}): {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
