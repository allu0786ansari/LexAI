"""
Write the API's OpenAPI schema to frontend/src/api/openapi.json, from which the
frontend's TypeScript types are generated (`npm run gen:api`). CI regenerates both
and fails if either differs from what is committed.

Usage (from backend/): python scripts/export_openapi.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lexai.api.app import create_app
from lexai.core.settings import REPO_ROOT, APISettings, ObservabilitySettings, Settings

OUT = REPO_ROOT / "frontend" / "src" / "api" / "openapi.json"


def schema() -> str:
    settings = Settings(api=APISettings(environment="ci"), obs=ObservabilitySettings())
    return json.dumps(create_app(settings).openapi(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the committed file is out of date")
    args = parser.parse_args()
    text = schema()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is out of date: run python scripts/export_openapi.py && npm run gen:api")
            sys.exit(1)
        print("OpenAPI schema up to date")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    Path(OUT).write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
