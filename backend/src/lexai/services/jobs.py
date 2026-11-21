"""Background jobs started from the admin API (one reindex at a time)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from lexai.core.logging import get_logger
from lexai.domain.models import ReindexResponse

logger = get_logger(__name__)


class JobBusy(Exception):
    pass


class JobRunner:
    def __init__(self) -> None:
        self.jobs: dict[str, ReindexResponse] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, work: Callable[[], Awaitable[str]]) -> ReindexResponse:
        """Run `work` (returns a status line) in the background; JobBusy if one is running."""
        if self.busy:
            raise JobBusy()
        job = ReindexResponse(job_id=uuid.uuid4().hex, status="queued")
        self.jobs[job.job_id] = job

        async def run() -> None:
            job.status = "running"
            job.started_at = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                job.detail = await work()
                job.status = "succeeded" if job.detail.startswith("live") else "failed"
            except Exception as exc:
                logger.exception("job_failed", job_id=job.job_id, error=repr(exc))
                job.status, job.detail = "failed", f"{type(exc).__name__}: {exc}"[:500]
            finally:
                job.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
                logger.info("job_finished", job_id=job.job_id, status=job.status, detail=job.detail)

        self._task = asyncio.create_task(run())
        return job

    async def aclose(self) -> None:
        if self.busy and self._task is not None:
            self._task.cancel()
