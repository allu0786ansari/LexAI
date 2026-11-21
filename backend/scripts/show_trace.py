"""
Show one request's trace from the local span file (LEXAI_OBS_TRACE_FILE), as a tree
with the time spent in each stage and what each stage produced.

Usage (from backend/):
    python scripts/show_trace.py                      # the last chat request
    python scripts/show_trace.py --request-id <id>    # by X-Request-ID / meta.request_id
    python scripts/show_trace.py --trace-id <id>      # by meta.trace_id
    python scripts/show_trace.py --list               # recent requests
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lexai.core.settings import ObservabilitySettings

SHOW = {
    "lexai.chat": [
        "lexai.intent",
        "lexai.applicable_code",
        "lexai.outcome",
        "lexai.prompt_version",
        "lexai.question",
    ],
    "lexai.condense": ["lexai.standalone_query"],
    "lexai.retrieve": ["lexai.retrieval.labels", "lexai.retrieval.scores", "lexai.retrieval.best_dense"],
    "lexai.context": ["lexai.context.passages", "lexai.context.chars"],
    "lexai.generate": [
        "gen_ai.request.model",
        "lexai.time_to_first_token_ms",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    ],
    "lexai.verify": ["lexai.verification.valid", "lexai.verification.coverage", "lexai.verification.flags"],
}


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"no trace file at {path}: set LEXAI_OBS_TRACE_FILE and send a request")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fmt(value: Any) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], float):
            return "[" + ", ".join(f"{v:.3f}" for v in value[:8]) + (", ..." if len(value) > 8 else "") + "]"
        return "[" + ", ".join(map(str, value[:8])) + (", ..." if len(value) > 8 else "") + "]"
    text = str(value)
    return text if len(text) <= 160 else text[:157] + "..."


def tree(spans: list[dict[str, Any]]) -> list[str]:
    children: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    ids = {s["span_id"] for s in spans}
    for s in spans:
        children[s["parent_id"] if s["parent_id"] in ids else None].append(s)
    lines: list[str] = []

    def walk(span: dict[str, Any], depth: int) -> None:
        mark = "" if span["status"] != "ERROR" else "  [ERROR]"
        lines.append(f"{'  ' * depth}{span['name']:<{44 - 2 * depth}} {span['duration_ms']:>10.1f} ms{mark}")
        for key in SHOW.get(span["name"], []):
            if key in span["attributes"]:
                lines.append(f"{'  ' * depth}    {key.split('.')[-1]}: {fmt(span['attributes'][key])}")
        for child in sorted(children[span["span_id"]], key=lambda c: c["start_ns"]):
            walk(child, depth + 1)

    for root in sorted(children[None], key=lambda c: c["start_ns"]):
        walk(root, 0)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--request-id")
    parser.add_argument("--trace-id")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    path = args.file or ObservabilitySettings().trace_file or Path("var/traces.jsonl")
    spans = load(path)
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(s)

    def request_id(trace: list[dict[str, Any]]) -> str:
        return next(
            (
                s["attributes"].get("lexai.request_id", "")
                for s in trace
                if "lexai.request_id" in s["attributes"]
            ),
            "",
        )

    chats = [t for t, ss in by_trace.items() if any(s["name"] == "lexai.chat" for s in ss)]
    if args.list:
        for t in chats[-20:]:
            chat = next(s for s in by_trace[t] if s["name"] == "lexai.chat")
            attrs = chat["attributes"]
            print(
                f"{t}  request {request_id(by_trace[t])}  {chat['duration_ms']:>9.0f} ms  "
                f"{attrs.get('lexai.outcome', '')}  {fmt(attrs.get('lexai.question', ''))}"
            )
        return
    if args.trace_id:
        trace_id = args.trace_id
    elif args.request_id:
        trace_id = next((t for t, ss in by_trace.items() if request_id(ss) == args.request_id), "")
    else:
        trace_id = chats[-1] if chats else ""
    if trace_id not in by_trace:
        raise SystemExit("trace not found (spans are written a few seconds after the request ends)")
    print(f"trace {trace_id}  request {request_id(by_trace[trace_id])}")
    print("\n".join(tree(by_trace[trace_id])))


if __name__ == "__main__":
    main()
