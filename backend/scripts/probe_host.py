"""
Can this container reach the model server on the host? Run inside the API image
(`deploy\\lexai.bat network`).

Tries the configured address (host.docker.internal, mapped by Compose to
LEXAI_HOST_GATEWAY, default `host-gateway`) and the other ways a container can name the
host: Rancher Desktop's and Docker Desktop's DNS names and the DNS server of the VM, which
on WSL2 is usually the Windows host. If only an alternative answers, prints
`LEXAI_HOST_GATEWAY=<ip>` for lexai.bat to save. Exit 0 when the configured address works.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from lexai.core.settings import get_settings

ALTERNATIVE_NAMES = ["host.rancher-desktop.internal", "host.docker.internal.", "gateway.docker.internal"]


def resolve(name: str) -> str | None:
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def answers(ip: str, port: int) -> str:
    try:
        with socket.create_connection((ip, port), timeout=3):
            return "open"
    except TimeoutError:
        return "timeout (firewall?)"
    except ConnectionRefusedError:
        return "refused (nothing listening there)"
    except OSError as exc:
        return type(exc).__name__


def nameservers() -> list[str]:
    try:
        lines = Path("/etc/resolv.conf").read_text().splitlines()
    except OSError:
        return []
    out = [line.split()[1] for line in lines if line.startswith("nameserver") and len(line.split()) > 1]
    return [ip for ip in out if not ip.startswith("127.")]


def main() -> None:
    url = get_settings().llm.base_url
    parsed = urlparse(url)
    host, port = parsed.hostname or "host.docker.internal", parsed.port or 11434
    candidates: list[tuple[str, str | None]] = [(f"{host} (configured)", resolve(host))]
    candidates += [(name, resolve(name)) for name in ALTERNATIVE_NAMES]
    candidates += [(f"DNS server {ip}", ip) for ip in nameservers()]

    print(f"model server URL: {url}")
    working: list[str] = []
    seen: set[str] = set()
    for label, ip in candidates:
        if ip is None:
            print(f"  {label:42} does not resolve")
            continue
        state = answers(ip, port) if ip not in seen else "(same address as above)"
        seen.add(ip)
        print(f"  {label:42} -> {ip:15} port {port}: {state}")
        if state == "open":
            working.append(ip)

    configured_ip = candidates[0][1]
    if configured_ip and configured_ip in working:
        with urllib.request.urlopen(f"{url.rstrip('/')}/models", timeout=10) as response:
            models = [m["id"] for m in json.loads(response.read())["data"]]
        print(f"[PASS] the model server answers at {url}: {len(models)} models")
        return
    if working:
        print(f"[FAIL] the configured address does not answer, but {working[0]} does.")
        print(f"LEXAI_HOST_GATEWAY={working[0]}")
        sys.exit(3)
    print(
        "[FAIL] no address of the host answers on port "
        f"{port}. Ollama on Windows probably listens on 127.0.0.1 only, or Windows Firewall\n"
        "blocks the WSL network. Fix: deploy\\ollama-for-docker.bat from an Administrator prompt."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
