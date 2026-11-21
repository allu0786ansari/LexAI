"""The Compose deployment keeps its security and operability properties (checked without Docker)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lexai.core.settings import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((DEPLOY / name).read_text(encoding="utf-8"))
    return data


def env_keys(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def test_images_are_pinned() -> None:
    compose = load("compose.yaml")
    for name, svc in compose["services"].items():
        image = svc["image"]
        assert not image.endswith(":latest") and ":" in image, f"{name}: {image}"
    gpu = load("compose.gpu.yaml")
    assert gpu["services"]["vllm"]["image"].split(":")[1].startswith("v")


def test_the_api_runs_hardened_and_reaches_only_internal_services() -> None:
    api = load("compose.yaml")["services"]["api"]
    assert api["read_only"] is True and api["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in api["security_opt"]
    assert "ports" not in api  # only nginx is published
    env = api["environment"]
    assert env["LEXAI_QDRANT_URL"] == "http://qdrant:6333"
    assert env["LEXAI_API_REDIS_URL"].startswith("redis://redis:")
    assert env["LEXAI_API_ENVIRONMENT"] == "production"  # no /docs
    assert any(v.endswith(":ro") and v.startswith("../data:") for v in api["volumes"])


def test_only_the_web_port_is_public() -> None:
    services = load("compose.yaml")["services"]
    for name, svc in services.items():
        for port in svc.get("ports", []):
            if name == "web":
                assert port.startswith("${LEXAI_WEB_BIND")
            else:
                assert port.startswith("127.0.0.1:"), f"{name} publishes {port} on every interface"


def test_compose_env_example_matches_the_evaluated_configuration() -> None:
    env = env_keys(DEPLOY / ".env.example")
    laptop = env_keys(DEPLOY / "env" / "laptop-8gb.env")
    for key in (
        "LEXAI_EMBED_MODEL",
        "LEXAI_EMBED_DIMENSION",
        "LEXAI_EMBED_QUERY_PREFIX",
        "LEXAI_EMBED_DOCUMENT_PREFIX",
        "LEXAI_RERANK_ENABLED",
        "LEXAI_CORPUS_MAX_CHUNK_CHARS",
        "LEXAI_CORPUS_CHUNK_OVERLAP_CHARS",
        "LEXAI_QUERY_ABSTAIN_MIN_DENSE",
        "LEXAI_LLM_MODEL",
    ):
        assert env[key] == laptop[key], key
    assert env["COMPOSE_PROFILES"] == "ollama"
    assert "change-me" in env["LEXAI_API_ADMIN_API_KEY"]  # placeholder, never a real secret


def test_the_laptop_overlay_uses_host_ollama_and_stays_local() -> None:
    env = env_keys(DEPLOY / "env" / "laptop-docker.env")
    assert env["COMPOSE_PROFILES"] == ""  # no Ollama container
    assert env["LEXAI_LLM_BASE_URL"].startswith("http://host.docker.internal:11434")
    assert env["LEXAI_WEB_BIND"] == "127.0.0.1"
    overlay = load("compose.laptop.yaml")
    assert overlay["services"]["api"]["env_file"][0]["path"] == "env/laptop-8gb.env"
