"""
Download the manifest's `urls` sources into data/raw/ and pin them.

For each downloaded document:
- if data/raw/<id>.pdf exists and matches its lock entry: nothing to do;
- otherwise try each URL in order; accept the first response that is a PDF
  and, when the document is already pinned, has the pinned SHA-256;
- record new pins in data/manifest.lock.json.

A changed upstream file is refused unless `--update` is given, so a silent
change on a government site cannot change what LexAI indexes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from lexai.core.logging import get_logger
from lexai.core.settings import CorpusSettings
from lexai.ingestion.manifest import (
    DocumentSpec,
    LockEntry,
    read_bundled_sums,
    read_lock,
    sha256_file,
    write_lock,
)

logger = get_logger(__name__)
USER_AGENT = "Mozilla/5.0 (compatible; LexAI-ingest/0.3; legal research corpus builder)"


@dataclass
class FetchResult:
    doc_id: str
    status: str  # cached | downloaded | bundled-ok | failed
    detail: str = ""
    errors: list[str] = field(default_factory=list)


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    body = response.content
    if not body.startswith(b"%PDF"):
        kind = response.headers.get("content-type", "unknown")
        raise ValueError(f"not a PDF (content-type {kind}, {len(body)} bytes)")
    return body


async def fetch_documents(
    docs: list[DocumentSpec],
    settings: CorpusSettings,
    *,
    update: bool = False,
    client: httpx.AsyncClient | None = None,
) -> list[FetchResult]:
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    lock = read_lock(settings.lock_file)
    sums = read_bundled_sums(settings.bundled_dir)
    results: list[FetchResult] = []
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.download_timeout_s, connect=20.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        verify=settings.verify_tls,
    )
    try:
        for spec in docs:
            if not spec.downloaded:
                path = settings.bundled_dir / str(spec.file)
                ok = path.exists() and sums.get(str(spec.file)) == sha256_file(path)
                results.append(
                    FetchResult(
                        spec.id,
                        "bundled-ok" if ok else "failed",
                        "" if ok else f"{spec.file} missing or checksum mismatch",
                    )
                )
                continue

            dest = settings.raw_dir / f"{spec.id}.pdf"
            pinned = lock.get(spec.id)
            if dest.exists() and pinned and sha256_file(dest) == pinned.sha256:
                results.append(FetchResult(spec.id, "cached", pinned.url))
                continue

            result = FetchResult(spec.id, "failed")
            for url in spec.urls:
                try:
                    body = await _download(client, url)
                except (httpx.HTTPError, ValueError) as exc:
                    result.errors.append(f"{url}: {exc!r}"[:300])
                    logger.warning("download_failed", doc=spec.id, url=url, error=repr(exc))
                    continue
                digest = hashlib.sha256(body).hexdigest()
                if pinned and digest != pinned.sha256 and not update:
                    result.errors.append(
                        f"{url}: content differs from the pinned version (use --update to accept)"
                    )
                    continue
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(body)
                tmp.replace(dest)
                lock[spec.id] = LockEntry(
                    url=url,
                    sha256=digest,
                    bytes=len(body),
                    fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
                write_lock(settings.lock_file, lock)
                result.status, result.detail = "downloaded", f"{url} ({len(body) / 1e6:.1f} MB)"
                break
            results.append(result)
    finally:
        if own_client:
            await client.aclose()
    return results


def format_results(results: list[FetchResult]) -> str:
    lines = [f"{'document':34} {'status':11} detail", "-" * 90]
    for r in results:
        lines.append(f"{r.doc_id:34} {r.status:11} {r.detail}")
        lines.extend(f"{'':34} {'':11} ! {e}" for e in r.errors)
    failed = [r.doc_id for r in results if r.status == "failed"]
    lines.append("-" * 90)
    lines.append("FETCH: all sources present and verified" if not failed else f"FETCH FAILED for: {failed}")
    return "\n".join(lines)


def raw_path(spec: DocumentSpec, settings: CorpusSettings) -> Path:
    return settings.raw_dir / f"{spec.id}.pdf"
