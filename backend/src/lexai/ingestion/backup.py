"""
Backups of the search index on a Qdrant server.

A backup is one directory `lexai-<UTC time>/` holding a Qdrant snapshot of the live
collection and a `backup.json` with its build metadata and checksum. Restoring uploads
the snapshot (to the same or a fresh server), writes the metadata back and points the
live alias at it; running API replicas pick the restored index up on their next probe.

    lexai-ingest backup [--dir /backups] [--keep 7]
    lexai-ingest restore [--dir /backups] [latest | lexai-20261121T020000Z]

The corpus itself is in git (data/bundled) or re-downloadable and checksum-pinned
(data/manifest.lock.json), so the index is the only state worth backing up besides the
feedback database (a single SQLite file in the API's var volume).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from lexai.core.errors import ConfigurationError, LexAIError
from lexai.core.logging import get_logger
from lexai.core.settings import QdrantSettings
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore

logger = get_logger(__name__)

FORMAT = 1
PREFIX = "lexai-"
MANIFEST = "backup.json"


class BackupError(LexAIError):
    code = "backup_failed"


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    collection: str
    points: int
    size_bytes: int
    pruned: list[str]


def _http(settings: QdrantSettings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    if not settings.url:
        raise ConfigurationError(
            "Backups use the Qdrant server's snapshot API; set LEXAI_QDRANT_URL. With the embedded store, "
            f"stop the API and copy {settings.path} instead."
        )
    headers = {"api-key": settings.api_key} if settings.api_key else {}
    return httpx.AsyncClient(
        base_url=settings.url,
        headers=headers,
        timeout=httpx.Timeout(600, connect=10),
        transport=transport,
    )


def _check(response: httpx.Response, what: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise BackupError(f"{what}: Qdrant answered {response.status_code} {response.text[:300]}")
    body: dict[str, Any] = response.json() if response.content else {}
    return body


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def complete_backups(root: Path) -> list[Path]:
    """Finished backups, oldest first (names sort by time)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.name.startswith(PREFIX) and (p / MANIFEST).is_file())


def _prune(root: Path, keep: int) -> list[str]:
    removed = []
    for leftover in root.glob(f".{PREFIX}*.partial"):
        shutil.rmtree(leftover, ignore_errors=True)
        removed.append(leftover.name)
    backups = complete_backups(root)
    for old in backups[: max(0, len(backups) - keep)]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


async def backup(
    store: QdrantStore,
    root: Path,
    *,
    keep: int = 7,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> BackupInfo:
    live = await store.live_collection()
    if not live:
        raise BackupError("there is no live index to back up (run `lexai-ingest build` first)")
    meta = await store.read_meta(live)
    if meta is None:
        raise BackupError(f"the live collection {live} has no build metadata")
    points = await store.count(live)

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    name = f"{PREFIX}{stamp}"
    root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-shot CLI job; nothing else on this loop
    final = root / name
    if final.exists():
        raise BackupError(f"{final} already exists")
    partial = root / f".{name}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()

    async with _http(store.settings, transport) as http:
        created = _check(
            await http.post(f"/collections/{live}/snapshots", params={"wait": "true"}), "create snapshot"
        )
        snapshot = created["result"]["name"]
        target = partial / f"{live}.snapshot"
        try:
            async with http.stream("GET", f"/collections/{live}/snapshots/{snapshot}") as response:
                if response.status_code >= 400:
                    raise BackupError(f"download snapshot: Qdrant answered {response.status_code}")
                with target.open("wb") as fh:
                    async for block in response.aiter_bytes(1 << 20):
                        fh.write(block)
        finally:
            # The copy lives in the backup directory; don't let snapshots fill Qdrant's disk.
            await http.delete(f"/collections/{live}/snapshots/{snapshot}")

    expected = created["result"].get("checksum")
    actual = _sha256(target)
    size = target.stat().st_size
    if expected and expected != actual:
        raise BackupError(f"snapshot checksum mismatch: Qdrant {expected}, downloaded {actual}")
    manifest = {
        "format": FORMAT,
        "created": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "alias": store.settings.collection_alias,
        "collection": live,
        "points": points,
        "snapshot": target.name,
        "sha256": actual,
        "size_bytes": size,
        "meta": meta.model_dump(),
    }
    (partial / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    partial.rename(final)
    pruned = _prune(root, keep)
    logger.info("backup_written", path=str(final), collection=live, points=points, pruned=pruned)
    return BackupInfo(final, live, points, size, pruned)


def resolve(root: Path, which: str) -> Path:
    if which in ("", "latest"):
        backups = complete_backups(root)
        if not backups:
            raise BackupError(f"no backups in {root}")
        return backups[-1]
    path = Path(which)
    path = path if path.is_absolute() else root / which
    if not (path / MANIFEST).is_file():
        raise BackupError(f"{path} is not a complete backup (no {MANIFEST})")
    return path


async def restore(
    store: QdrantStore,
    path: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')!r}")
    snapshot = path / manifest["snapshot"]
    if _sha256(snapshot) != manifest["sha256"]:
        raise BackupError(f"{snapshot} is damaged (checksum differs from {MANIFEST})")
    collection = manifest["collection"]
    meta = BuildMeta.model_validate(manifest["meta"])
    if meta.collection != collection:
        raise BackupError(f"{MANIFEST} is inconsistent: meta is for {meta.collection}")
    previous = await store.live_collection()

    async with _http(store.settings, transport) as http:
        with snapshot.open("rb") as fh:
            _check(
                await http.post(
                    f"/collections/{collection}/snapshots/upload",
                    params={"priority": "snapshot", "wait": "true"},
                    files={"snapshot": (snapshot.name, fh, "application/octet-stream")},
                ),
                "upload snapshot",
            )
    points = await store.count(collection)
    if points != manifest["points"]:
        raise BackupError(f"restored {points} points, the backup has {manifest['points']}")
    await store.write_meta(meta)
    await store.set_live(collection)
    logger.info("backup_restored", path=str(path), collection=collection, previous=previous)
    return {"collection": collection, "points": points, "previous": previous, "created": manifest["created"]}
