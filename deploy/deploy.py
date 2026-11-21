#!/usr/bin/env python3
"""
Zero-surprise deploys with automatic rollback (Linux, macOS and Windows; needs only Python 3.11+
and Docker Compose on the host).

    python deploy/deploy.py --tag 1.2.0                 # server: pull, back up, switch, smoke, keep or roll back
    python deploy/deploy.py --tag local --no-pull       # images built on this machine
    python deploy/deploy.py --rollback                  # go back to the previous release
    python deploy/deploy.py --laptop --tag local --no-pull --min-cited 4   # laptop overlay

Steps: 1) pull the release images, 2) back up the live index, 3) replace the API and web
containers, 4) run scripts/smoke_release.py through nginx (readiness, UI, five questions),
5) on failure put the previous release back, smoke it again and exit 1.
The current and previous releases are recorded in deploy/var/release.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
STATE = DEPLOY / "var" / "release.json"


def log(msg: str) -> None:
    print(f"[deploy {datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


def env_file_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


class Compose:
    def __init__(self, env_file: Path, overlays: list[str]) -> None:
        self.base = ["docker", "compose", "--project-directory", str(DEPLOY), "--env-file", str(env_file),
                     "-f", str(DEPLOY / "compose.yaml")]
        for overlay in overlays:
            self.base += ["-f", str(DEPLOY / overlay)]
        self.env = dict(os.environ)
        if hasattr(os, "getuid"):  # jobs write to bind mounts as the invoking user
            self.env.setdefault("LEXAI_UID", str(os.getuid()))
            self.env.setdefault("LEXAI_GID", str(os.getgid()))

    def run(self, *args: str, tag: str, check: bool = False, capture: bool = False) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "LEXAI_IMAGE_TAG": tag}
        return subprocess.run(  # noqa: S603 - fixed docker compose argv
            [*self.base, *args], env=env, check=check, text=True,
            capture_output=capture, encoding="utf-8", errors="replace",
        )


def read_state() -> dict[str, object]:
    try:
        data: dict[str, object] = json.loads(STATE.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return {}


def write_state(current: str, previous: str | None) -> None:
    state = read_state()
    history = [h for h in state.get("history", []) if isinstance(h, dict)][-19:]  # type: ignore[union-attr]
    history.append({"tag": current, "at": datetime.now(UTC).isoformat(timespec="seconds")})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"current": current, "previous": previous, "history": history}, indent=2),
                     encoding="utf-8")


def switch(compose: Compose, tag: str) -> bool:
    log(f"starting api + web at {tag}")
    return compose.run("up", "-d", "--no-deps", "--force-recreate", "api", "web", tag=tag).returncode == 0


def smoke(compose: Compose, tag: str, ready_timeout: int, min_cited: int) -> bool:
    log(f"smoke test of {tag} (waiting up to {ready_timeout} s for readiness)")
    result = compose.run("run", "--rm", "--no-deps", "smoke", "--base-url", "http://web:8080",
                         "--wait-ready", str(ready_timeout), "--min-cited", str(min_cited), tag=tag)
    return result.returncode == 0


def save_logs(compose: Compose, tag: str) -> Path:
    out = DEPLOY / "var" / f"deploy-failure-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    logs = compose.run("logs", "--no-color", "--tail", "200", "api", "web", tag=tag, capture=True)
    out.write_text(logs.stdout + logs.stderr, encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tag", help="image tag to deploy (a release version, a commit SHA or 'local')")
    target.add_argument("--rollback", action="store_true", help="deploy the previous release again")
    parser.add_argument("--env-file", type=Path, help="Compose env file (default deploy/.env)")
    parser.add_argument("-f", "--overlay", action="append", default=[], help="extra compose file in deploy/")
    parser.add_argument("--laptop", action="store_true", help="laptop overlay and env (host Ollama)")
    parser.add_argument("--no-pull", action="store_true", help="images are built locally")
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-deploy index backup")
    parser.add_argument("--ready-timeout", type=int, default=300)
    parser.add_argument("--min-cited", type=int, default=0, help="answers that must cite a source (of 5)")
    args = parser.parse_args()

    if args.laptop:
        args.overlay = ["compose.laptop.yaml", *args.overlay]
        args.env_file = args.env_file or DEPLOY / "env" / "laptop-docker.env"
    env_file = args.env_file or DEPLOY / ".env"
    if not env_file.is_file():
        log(f"{env_file} not found: copy deploy/.env.example to deploy/.env and edit it")
        return 2
    compose = Compose(env_file, args.overlay)
    state = read_state()
    current = str(state.get("current") or env_file_value(env_file, "LEXAI_IMAGE_TAG") or "")
    if args.rollback:
        tag = str(state.get("previous") or "")
        if not tag:
            log("no previous release recorded in deploy/var/release.json")
            return 2
    else:
        tag = args.tag
    previous = current if current and current != tag else (str(state.get("previous") or "") or None)
    log(f"deploying {tag} (now running: {current or 'nothing recorded'}; rollback target: {previous or 'none'})")

    t0 = time.monotonic()
    if not args.no_pull:
        log("pulling images")
        if compose.run("pull", "api", "web", tag=tag).returncode != 0:
            log("pull failed: nothing was changed")
            return 1
    if not args.no_backup:
        log("backing up the live index")
        if compose.run("run", "--rm", "ingest", "backup", tag=current or tag).returncode != 0:
            log("backup failed or no index yet (continuing; restore needs an earlier backup)")

    if switch(compose, tag) and smoke(compose, tag, args.ready_timeout, args.min_cited):
        write_state(tag, previous)
        log(f"DEPLOYED {tag} in {time.monotonic() - t0:.0f} s")
        return 0

    failure_log = save_logs(compose, tag)
    log(f"{tag} FAILED its checks; container logs saved to {failure_log}")
    if not previous:
        log("no previous release to roll back to; the failed release is still running")
        return 1
    log(f"ROLLING BACK to {previous}")
    if switch(compose, previous) and smoke(compose, previous, args.ready_timeout, args.min_cited):
        write_state(previous, str(state.get("previous") or "") or None)
        log(f"ROLLED BACK to {previous} in {time.monotonic() - t0:.0f} s; {tag} was not deployed")
    else:
        log(f"ROLLBACK to {previous} ALSO FAILED: investigate now (docs/runbook.md)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
