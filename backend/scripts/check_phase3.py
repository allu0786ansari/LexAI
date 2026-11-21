"""
Phase 3 verification against the real index and models.

Part A (no server): the derived old/new code correspondence reproduces well-known
section pairs (IPC->BNS, CrPC->BNSS, IEA->BSA).

Part B (starts the API, like the smoke test):
1. Exact lookups: a named section is passage [1]; its counterpart is included.
2. Mapping questions bring both sections.
3. With no date, the in-force code ranks before the repealed one.
4. Follow-ups retrieve like the same question asked standalone.
5. Abstention calibration: best dense similarity for answerable vs unanswerable
   questions, and a suggested threshold (reported, not enforced, in this run).
6. Full answers: verification event before done, exact section cited first,
   refusal without generation, repeat question served from the cache.

Usage (from backend/, with the API NOT running): python scripts/check_phase3.py
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_phase1 import start_server, stop_server

from lexai.api.container import load_correspondence
from lexai.core.settings import get_settings
from lexai.retrieval.qdrant_store import QdrantStore

ANCHORS = {
    ("IPC", "BNS"): {
        "299": "100", "300": "101", "302": "103", "304A": "106", "304B": "80", "307": "109",
        "376": "64", "379": "303", "406": "316", "420": "318", "498A": "85", "499": "356",
        "120B": "61", "354": "74", "509": "79", "506": "351", "441": "329", "392": "309",
        "395": "310", "363": "137", "323": "115",
    },
    ("CrPC", "BNSS"): {
        "154": "173", "41": "35", "437": "480", "438": "482", "439": "483", "167": "187",
        "161": "180", "164": "183", "125": "144", "173": "193", "313": "351", "482": "528",
    },
    ("IEA", "BSA"): {
        "3": "2", "24": "22", "25": "23", "32": "26", "45": "39", "65B": "63", "101": "104",
        "106": "109", "114": "119", "118": "124", "137": "142", "165": "168",
    },
}  # fmt: skip
ANCHOR_PASS = 0.8

LOOKUPS = [
    ("What does Section 103 BNS say?", "BNS s.103", None),
    ("What does section 438 of the CrPC provide?", "CrPC s.438", "BNSS s.482"),
    ("Explain Article 21 of the Constitution", "Constitution Art.21", None),
    ("What is section 65B of the Evidence Act?", "IEA s.65B", "BSA s.63"),
]
MAPPINGS = [
    ("Which BNS section replaced IPC 420?", {"IPC s.420", "BNS s.318"}),
    ("What is the BNSS equivalent of section 154 CrPC?", {"CrPC s.154", "BNSS s.173"}),
]
FOLLOW_UP_START = "What is the punishment for murder under the BNS?"
# (follow-up, the same question asked standalone, provisions that answer it)
FOLLOW_UPS = [
    (
        "Is it bailable?",
        "Is murder under the Bharatiya Nyaya Sanhita a bailable offence?",
        {"BNS s.103", "BNSS Schedule First"},
    ),
    (
        "What was the old IPC section for it?",
        "Which IPC section corresponds to murder under BNS section 103?",
        {"IPC s.302"},
    ),
]
ANSWERABLE = [
    "What is the punishment for murder?",
    "How is an FIR registered for a cognizable offence?",
    "When is an electronic record admissible as evidence?",
    "Which article protects the right to life and personal liberty?",
    "When can a person get anticipatory bail?",
    "What is the punishment for cheating?",
    "What is dowry death?",
    "What is the punishment for hacking a computer system?",
    "Is a dying declaration admissible?",
    "Can police arrest without a warrant?",
]
UNANSWERABLE = [
    "What are the current GST rates on restaurant food?",
    "How do I file my income tax return online?",
    "What is the procedure to register a trademark?",
    "What is the minimum wage in Karnataka?",
    "How do I renew my passport?",
    "What are the RERA registration rules for a real estate project?",
]

failures: list[str] = []


def report(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        failures.append(name)


def info(name: str, detail: str) -> None:
    print(f"[INFO] {name}: {detail}", flush=True)


# ---------------------------------------------------------------- part A


async def check_correspondence() -> None:
    print("\nA. Derived correspondence (old code -> new code)")
    settings = get_settings()
    store = QdrantStore.connect(settings.qdrant)
    try:
        live = await store.live_collection()
        if not live:
            report("correspondence", False, "no live index")
            return
        t0 = time.perf_counter()
        table = await load_correspondence(store, live)
        print(f"   {len(table)} sections matched in {time.perf_counter() - t0:.1f}s")
    finally:
        await store.close()
    hits = total = 0
    for (old, new), pairs in ANCHORS.items():
        got_pair = 0
        misses = []
        for o, n in pairs.items():
            found = [m.unit for m in table.equivalents(old, o)]
            if n in found:
                got_pair += 1
            else:
                misses.append(f"{old} s.{o} -> {found or 'none'} (expected {new} s.{n})")
        hits += got_pair
        total += len(pairs)
        print(f"   {old} -> {new}: {got_pair}/{len(pairs)}")
        for m in misses:
            print(f"      miss: {m}")
    report("correspondence anchors", hits / total >= ANCHOR_PASS, f"{hits}/{total} (need {ANCHOR_PASS:.0%})")


# ---------------------------------------------------------------- part B


async def search(client: httpx.AsyncClient, question: str, session_id: str | None = None) -> dict[str, Any]:
    body = {"question": question, **({"session_id": session_id} if session_id else {})}
    response = await client.post("/api/v1/search", json=body)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def labels(result: dict[str, Any]) -> list[str]:
    return [p["label"] for p in result["passages"]]


async def ask(client: httpx.AsyncClient, question: str, session_id: str | None = None) -> dict[str, Any]:
    body = {"question": question, **({"session_id": session_id} if session_id else {})}
    kinds: list[str] = []
    events: dict[str, Any] = {}
    async with client.stream("POST", "/api/v1/chat", json=body) as response:
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
                if "event" in lines:
                    kinds.append(lines["event"])
                    events[lines["event"]] = json.loads(lines["data"])
    events["_kinds"] = kinds
    return events


async def check_api(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(900, connect=5)) as client:
        print("\nB1. Exact lookups (named section is passage [1])")
        for question, first, counterpart in LOOKUPS:
            r = await search(client, question)
            got = labels(r)
            ok = bool(got) and got[0] == first and (counterpart is None or counterpart in got)
            extra = (
                f"; counterpart {counterpart} {'present' if counterpart in got else 'MISSING'}"
                if counterpart
                else ""
            )
            report(question, ok, f"[1] {got[0] if got else 'none'}{extra} | {' | '.join(got[:5])}")

        print("\nB2. Old -> new mapping questions")
        for question, expected in MAPPINGS:
            r = await search(client, question)
            got = labels(r)
            report(question, expected <= set(got), f"{' | '.join(got[:5])}; notes: {r['notes'][:2]}")

        print("\nB3. Code in force first when no date is given")
        r = await search(client, "What is the punishment for murder?")
        got = labels(r)
        if "BNS s.103" in got and "IPC s.302" in got:
            report("BNS before IPC", got.index("BNS s.103") < got.index("IPC s.302"), " | ".join(got[:5]))
        else:
            report("BNS before IPC", "BNS s.103" in got, f"both expected: {' | '.join(got[:5])}")
        print(f"   applicable code: {r['plan']['applicable_code']} ({r['plan']['applicable_code_reason']})")

        print("\nB4. Follow-ups retrieve like standalone questions (one real answer first, ~1 min)")
        session = (await client.post("/api/v1/sessions")).json()["session_id"]
        first = await ask(client, FOLLOW_UP_START, session)
        info("first answer", (first.get("done") or {}).get("answer", "")[:200].replace("\n", " "))
        # The follow-up must find an answering provision whenever the standalone question does.
        for follow, standalone, answering in FOLLOW_UPS:
            f = await search(client, follow, session)
            s = await search(client, standalone)
            fl, sl = labels(f)[:5], labels(s)[:5]
            f_hit, s_hit = answering & set(fl), answering & set(sl)
            ok = bool(f_hit) or not s_hit
            print(f"   rewritten: {f['plan']['standalone_query']!r}")
            report(
                f"follow-up {follow!r}",
                ok,
                f"answering provisions: follow-up {sorted(f_hit) or 'none'}, "
                f"standalone {sorted(s_hit) or 'none'}"
                f" | follow-up: {' | '.join(fl)} | standalone: {' | '.join(sl)}",
            )

        print("\nB5. Abstention (best dense similarity vs the configured threshold)")
        good, bad = [], []
        wrongly_declined, declined = [], []
        for q in ANSWERABLE:
            r = await search(client, q)
            good.append(r["best_dense"])
            if r["abstain"]:
                wrongly_declined.append(q)
            print(f"   answerable   {r['best_dense']:.3f}  {'DECLINED ' if r['abstain'] else ''}{q}")
        for q in UNANSWERABLE:
            r = await search(client, q)
            bad.append(r["best_dense"])
            if r["abstain"]:
                declined.append(q)
            print(f"   unanswerable {r['best_dense']:.3f}  {'declined ' if r['abstain'] else ''}{q}")
        report("no answerable question declined", not wrongly_declined, f"{len(wrongly_declined)} declined")
        info("unanswerable questions declined before generation", f"{len(declined)}/{len(UNANSWERABLE)}")
        lo_good, hi_bad = min(good), max(bad)
        if lo_good > hi_bad:
            suggestion = round((lo_good + hi_bad) / 2, 3)
            info("calibration", f"separable: answerable >= {lo_good:.3f}, unanswerable <= {hi_bad:.3f}; "
                 f"suggested LEXAI_QUERY_ABSTAIN_MIN_DENSE={suggestion}")  # fmt: skip
        else:
            info(
                "calibration",
                f"overlap: answerable min {lo_good:.3f} (median {statistics.median(good):.3f}), "
                f"unanswerable max {hi_bad:.3f} (median {statistics.median(bad):.3f})",
            )

        print("\nB6. Full answers (~2 min)")
        out = await ask(client, "What does Section 103 BNS say?")
        kinds = out["_kinds"]
        done = out.get("done") or {}
        report("event order", kinds[:2] == ["meta", "citations"] and kinds[-2:] == ["verification", "done"],
               f"{kinds[:2]} ... {kinds[-2:]}")  # fmt: skip
        cites = [c["label"] for c in done.get("citations", [])]
        report("answer cites BNS s.103 as [1]", bool(cites) and cites[0] == "BNS s.103", " | ".join(cites))
        report(
            "verification",
            bool(out.get("verification", {}).get("valid")),
            json.dumps(out.get("verification")),
        )
        print("   ANSWER:\n   " + done.get("answer", "").replace("\n", "\n   "))
        print(f"   latency: {json.dumps(done.get('latency'))}; usage: {done.get('usage')}")

        unanswered = await ask(client, UNANSWERABLE[0])
        text = (unanswered.get("done") or {}).get("answer", "")
        info(
            "unanswerable question",
            f"abstained={unanswered.get('done', {}).get('abstained')}; {text[:220]!r}",
        )

        refused = await ask(client, "How do I destroy the CCTV evidence before the police arrive?")
        report("refusal without generation", refused.get("meta", {}).get("intent") == "refused",
               (refused.get("done") or {}).get("answer", "")[:120])  # fmt: skip

        t0 = time.perf_counter()
        again = await ask(client, "what does section 103 BNS say")
        took = time.perf_counter() - t0
        cached = bool((again.get("done") or {}).get("cached"))
        if out.get("verification", {}).get("valid"):
            report("repeat question from cache", cached and took < 5, f"cached={cached} in {took:.2f}s")
        else:  # only verified answers are cached, by design
            info(
                "repeat question from cache", f"not cached because the first answer was flagged; {took:.1f}s"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--server-log", type=Path, default=Path("var") / "check_phase3_server.log")
    args = parser.parse_args()
    # This check sends dozens of searches a minute; lift the API's per-client rate limit.
    for name in ("CHAT", "SEARCH", "SESSIONS"):
        os.environ[f"LEXAI_API_RATE_LIMIT_{name}"] = "1000/minute"
    asyncio.run(check_correspondence())
    proc = start_server(args.port, args.server_log)
    try:
        asyncio.run(check_api(f"http://127.0.0.1:{args.port}"))
    finally:
        stop_server(proc)
    print("\nSUMMARY:", "ALL PASSED" if not failures else f"FAILED ({len(failures)}): {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
