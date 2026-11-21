"""
Download the manifest's `urls` sources into data/raw/ and pin them.

For each downloaded document:
- if data/raw/<id>.pdf exists and matches its lock entry: nothing to do;
- otherwise try the pinned URL first, then each manifest URL in order; accept
  the first response that is a PDF and, when the document is already pinned,
  has the pinned SHA-256;
- a URL that answers 429/5xx, times out or drops the connection is retried with
  backoff before moving on (government sites are often overloaded);
- record new pins in data/manifest.lock.json.

A changed upstream file is refused unless `--update` is given, so a silent
change on a government site cannot change what LexAI indexes.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
# Some government sites refuse requests that do not look like a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36 LexAI-ingest/0.3"
    ),
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-IN,en;q=0.9",
}
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass
class FetchResult:
    doc_id: str
    status: str  # cached | downloaded | bundled-ok | failed
    detail: str = ""
    errors: list[str] = field(default_factory=list)


class DownloadError(Exception):
    """One URL failed; the message is a short, human-readable reason."""


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out ({type(exc).__name__})"
    if isinstance(exc, httpx.TransportError):
        return f"connection failed ({type(exc).__name__}: {exc})"[:160]
    return str(exc)[:160]


async def _download_once(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    body = response.content
    if not body.startswith(b"%PDF"):
        kind = response.headers.get("content-type", "unknown")
        raise DownloadError(f"not a PDF (content-type {kind}, {len(body)} bytes)")
    return body


async def _download(client: httpx.AsyncClient, url: str, attempts: int, backoff_s: float) -> bytes:
    for attempt in range(1, attempts + 1):
        try:
            return await _download_once(client, url)
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in RETRY_STATUSES
            error: Exception = exc
        except httpx.TransportError as exc:  # timeouts, refused or reset connections
            retryable, error = True, exc
        if not retryable or attempt == attempts:
            tries = f" after {attempt} attempts" if attempt > 1 else ""
            raise DownloadError(_describe(error) + tries) from error
        wait = backoff_s * 3 ** (attempt - 1)
        logger.warning("download_retry", url=url, attempt=attempt, error=_describe(error), wait_s=wait)
        print(f"  {url}: {_describe(error)}; retrying in {wait:.0f}s ...", flush=True)
        await asyncio.sleep(wait)
    raise AssertionError("unreachable")


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
        headers=HEADERS,
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
            urls = list(spec.urls)
            if pinned and pinned.url in urls:  # the source that produced the pin first
                urls.remove(pinned.url)
                urls.insert(0, pinned.url)
            for url in urls:
                print(f"{spec.id}: downloading {url}", flush=True)
                try:
                    body = await _download(
                        client, url, settings.download_attempts, settings.download_backoff_s
                    )
                except DownloadError as exc:
                    result.errors.append(f"{url}: {exc}")
                    logger.warning("download_failed", doc=spec.id, url=url, error=str(exc))
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
