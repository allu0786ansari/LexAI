"""Index backups: snapshot download, checksums, retention, and restore onto a fresh server."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from qdrant_client import models

from lexai.core.errors import ConfigurationError
from lexai.core.settings import QdrantSettings, Settings
from lexai.ingestion import backup as backups
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore

SNAPSHOT = b"QDRANT-SNAPSHOT-" * 1000


def as_server(store: QdrantStore) -> QdrantStore:
    """The embedded store, presented as a server (the snapshot API itself is mocked)."""
    return QdrantStore(store._client, store.settings.model_copy(update={"url": "http://qdrant.test"}))


class FakeQdrantHTTP:
    """The snapshot endpoints of a Qdrant server. An upload copies `source` into `target`."""

    def __init__(self, source: QdrantStore | None = None, target: QdrantStore | None = None) -> None:
        self.source, self.target = source, target
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.uploaded = b""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.method == "POST" and path.endswith("/snapshots"):
            sha = hashlib.sha256(SNAPSHOT).hexdigest()
            return httpx.Response(200, json={"result": {"name": "snap-1.snapshot", "checksum": sha}})
        if request.method == "GET" and "/snapshots/" in path:
            return httpx.Response(200, content=SNAPSHOT)
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(200, json={"result": True})
        if request.method == "POST" and path.endswith("/snapshots/upload"):
            self.uploaded = await request.aread()
            assert request.url.params["priority"] == "snapshot"
            name = path.split("/")[2]
            assert self.source is not None and self.target is not None
            await copy_collection(self.source, self.target, name)
            return httpx.Response(200, json={"result": True})
        return httpx.Response(404, text="unexpected call")


async def copy_collection(source: QdrantStore, target: QdrantStore, name: str) -> None:
    info = source._client.get_collection(name)
    target._client.create_collection(
        name,
        vectors_config=info.config.params.vectors,
        sparse_vectors_config=info.config.params.sparse_vectors,
    )
    points, _ = source._client.scroll(name, limit=10_000, with_payload=True, with_vectors=True)
    target._client.upsert(
        name, points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points]
    )


async def test_backup_downloads_the_live_snapshot_and_cleans_up(
    live_store: tuple[QdrantStore, str, BuildMeta], tmp_path: Path
) -> None:
    store, name, meta = live_store
    fake = FakeQdrantHTTP()
    info = await backups.backup(as_server(store), tmp_path / "b", transport=httpx.MockTransport(fake))

    assert info.collection == name and info.points == await store.count(name)
    assert (info.path / f"{name}.snapshot").read_bytes() == SNAPSHOT
    manifest = json.loads((info.path / "backup.json").read_text(encoding="utf-8"))
    assert manifest["collection"] == name and manifest["meta"]["docs"] == meta.model_dump()["docs"]
    assert fake.deleted == [f"/collections/{name}/snapshots/snap-1.snapshot"]  # not left on the server
    assert not list((tmp_path / "b").glob(".*partial"))


async def test_old_backups_and_interrupted_ones_are_removed(
    live_store: tuple[QdrantStore, str, BuildMeta], tmp_path: Path
) -> None:
    store, _, _ = live_store
    root = tmp_path / "b"
    (root / ".lexai-19990101T000000Z.partial").mkdir(parents=True)
    t0 = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
    for day in range(4):
        info = await backups.backup(
            as_server(store),
            root,
            keep=2,
            transport=httpx.MockTransport(FakeQdrantHTTP()),
            now=t0 + timedelta(days=day),
        )
    kept = [p.name for p in backups.complete_backups(root)]
    assert kept == ["lexai-20261103T020000Z", "lexai-20261104T020000Z"]
    assert info.pruned == ["lexai-20261102T020000Z"]
    assert not (root / ".lexai-19990101T000000Z.partial").exists()
    assert backups.resolve(root, "latest").name == "lexai-20261104T020000Z"


async def test_restore_onto_a_fresh_server(
    live_store: tuple[QdrantStore, str, BuildMeta], settings: Settings, tmp_path: Path
) -> None:
    store, name, meta = live_store
    root = tmp_path / "b"
    info = await backups.backup(as_server(store), root, transport=httpx.MockTransport(FakeQdrantHTTP()))

    fresh = QdrantStore.connect(QdrantSettings(path=tmp_path / "fresh"))
    try:
        assert await fresh.live_collection() is None
        fake = FakeQdrantHTTP(source=store, target=fresh)
        result = await backups.restore(
            as_server(fresh), backups.resolve(root, "latest"), transport=httpx.MockTransport(fake)
        )
        assert SNAPSHOT in fake.uploaded
        assert result == {
            "collection": name,
            "points": info.points,
            "previous": None,
            "created": result["created"],
        }
        assert await fresh.live_collection() == name
        restored = await fresh.read_meta(name)
        assert restored is not None and restored.docs == meta.docs
    finally:
        await fresh.close()


async def test_a_damaged_backup_is_refused(
    live_store: tuple[QdrantStore, str, BuildMeta], tmp_path: Path
) -> None:
    store, name, _ = live_store
    info = await backups.backup(as_server(store), tmp_path, transport=httpx.MockTransport(FakeQdrantHTTP()))
    (info.path / f"{name}.snapshot").write_bytes(b"truncated")
    with pytest.raises(backups.BackupError, match="damaged"):
        await backups.restore(as_server(store), info.path, transport=httpx.MockTransport(FakeQdrantHTTP()))


async def test_a_checksum_mismatch_on_download_fails_the_backup(
    live_store: tuple[QdrantStore, str, BuildMeta], tmp_path: Path
) -> None:
    store, _, _ = live_store

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=SNAPSHOT[:100])  # cut off
        return await FakeQdrantHTTP()(request)

    with pytest.raises(backups.BackupError, match="checksum"):
        await backups.backup(as_server(store), tmp_path, transport=httpx.MockTransport(handler))
    assert backups.complete_backups(tmp_path) == []


async def test_backups_need_a_live_index(settings: Settings, tmp_path: Path) -> None:
    store = QdrantStore.connect(settings.qdrant)
    try:
        with pytest.raises(backups.BackupError, match="no live index"):
            await backups.backup(as_server(store), tmp_path)
    finally:
        await store.close()
    with pytest.raises(backups.BackupError, match="no backups"):
        backups.resolve(tmp_path / "none", "latest")


async def test_the_embedded_store_has_no_snapshot_api(
    live_store: tuple[QdrantStore, str, BuildMeta], tmp_path: Path
) -> None:
    store, _, _ = live_store
    with pytest.raises(ConfigurationError, match="LEXAI_QDRANT_URL"):
        await backups.backup(store, tmp_path)
