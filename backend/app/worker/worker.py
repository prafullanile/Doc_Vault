"""The processing worker: claims jobs from the PostgreSQL queue and runs pipeline stages.

- Bounded concurrency: at most ``worker_concurrency`` jobs in flight (§31).
- Heartbeats extend the lease while a stage runs. If ownership is lost, or the job is
  cancelled, the stage is cancelled too.
- Graceful shutdown: stop claiming, let in-flight jobs finish. A hard kill is also safe,
  because the leases expire and the reaper (run by every worker) requeues the jobs.
"""

import asyncio
import contextlib
import os
import socket
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.database.models  # noqa: F401  (registers every mapper; FKs span all tables)
from app.core.config import Settings
from app.database.session import bind_tenant
from app.documents.models import DocumentStatus
from app.documents.storage import StorageBackend, StorageError
from app.processing import queue
from app.processing.errors import PermanentError, RetryableError
from app.processing.models import JobStatus
from app.processing.queue import ClaimedJob
from app.processing.stages import PIPELINE_VERSION, STAGES, Stage, StageContext, next_stage

log = structlog.get_logger(__name__)


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: StorageBackend,
        *,
        worker_id: str | None = None,
        stages: dict[Any, Stage] | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.storage = storage
        self.worker_id = worker_id or default_worker_id()
        self.stages = stages or STAGES
        self._stopping = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()

    # --- lifecycle -----------------------------------------------------------------------------

    def request_stop(self) -> None:
        log.info("worker_stopping", in_flight=len(self._in_flight))
        self._stopping.set()

    async def run(self) -> None:
        log.info(
            "worker_started", worker_id=self.worker_id, concurrency=self.settings.worker_concurrency
        )
        reaper = asyncio.create_task(self._reaper_loop())
        slots = asyncio.Semaphore(self.settings.worker_concurrency)
        try:
            while not self._stopping.is_set():
                await slots.acquire()
                if self._stopping.is_set():
                    slots.release()
                    break
                job = await self._claim_safely()
                if job is None:
                    slots.release()
                    await self._sleep(self.settings.worker_poll_interval_seconds)
                    continue
                task = asyncio.create_task(self.process(job))
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)
                task.add_done_callback(lambda _: slots.release())
        finally:
            reaper.cancel()
            if self._in_flight:
                await asyncio.gather(*self._in_flight, return_exceptions=True)
            log.info("worker_stopped", worker_id=self.worker_id)

    async def run_once(self) -> ClaimedJob | None:
        """Claims and fully processes at most one job. Used by tests and one-shot tooling."""
        job = await self._claim_safely()
        if job is not None:
            await self.process(job)
        return job

    async def reap(self) -> int:
        async with self.sessionmaker() as session:
            count = await queue.reap_expired(session)
        if count:
            log.warning("reaped_expired_jobs", count=count)
        return count

    async def _reaper_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.reap()
            except Exception:
                log.exception("reaper_failed")
            await self._sleep(self.settings.reaper_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _claim_safely(self) -> ClaimedJob | None:
        try:
            async with self.sessionmaker() as session:
                return await queue.claim(session, self.worker_id, self.settings.job_lease_seconds)
        except Exception:  # database blip: back off, don't crash the worker
            log.exception("claim_failed")
            await self._sleep(self.settings.worker_poll_interval_seconds * 5)
            return None

    # --- one job -------------------------------------------------------------------------------

    async def process(self, job: ClaimedJob) -> None:
        bound = log.bind(
            job_id=str(job.id),
            job_type=job.job_type,
            attempt=job.attempt,
            tenant_id=str(job.tenant_id),
            document_id=str(job.document_id),
        )
        stage = self.stages[job.job_type]
        ctx = StageContext(job, self.settings, self.storage, self.sessionmaker)
        bound.info("job_started")

        work: asyncio.Task[Any] = asyncio.create_task(stage.execute(ctx))
        stop_reason = await self._supervise(job, work)

        try:
            result = await work
        except asyncio.CancelledError:
            if stop_reason is None:  # the worker itself is being torn down
                raise
            if stop_reason == "cancel_requested":
                async with self.sessionmaker() as session:
                    await queue.mark_cancelled(session, job)
                bound.info("job_cancelled")
            else:
                bound.warning("job_abandoned", reason=stop_reason)  # lease lost: reaper owns it
            return
        except PermanentError as exc:
            await self._fail(job, exc.code, exc.message, permanent=True)
            bound.warning("job_failed_permanently", error_code=exc.code, error=exc.message)
            return
        except (RetryableError, StorageError, OSError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, RetryableError) else type(exc).__name__.upper()
            status = await self._fail(job, code, str(exc), permanent=False)
            bound.warning("job_failed", error_code=code, error=str(exc), new_status=status)
            return
        except Exception as exc:  # unknown bug: retry, and let max_attempts bound the damage
            status = await self._fail(job, "UNEXPECTED_ERROR", repr(exc), permanent=False)
            bound.exception("job_crashed", new_status=status)
            return

        if await self._complete(job, stage, ctx, result):
            bound.info("job_completed")
        else:
            bound.warning("job_result_discarded", reason="lease lost before commit")

    async def _supervise(self, job: ClaimedJob, work: asyncio.Task[Any]) -> str | None:
        """Heartbeats until the stage finishes. Cancels it if ownership is lost or a cancel is
        requested; returns that reason, or None if the stage ended on its own."""
        interval = max(self.settings.job_lease_seconds / 3, 0.05)
        while True:
            done, _ = await asyncio.wait({work}, timeout=interval)
            if done:
                return None
            try:
                async with self.sessionmaker() as session:
                    owned, cancel = await queue.heartbeat(
                        session, job, self.settings.job_lease_seconds
                    )
            except Exception:
                log.exception("heartbeat_failed", job_id=str(job.id))
                continue  # the lease has slack for a missed beat or two
            if not owned or cancel:
                work.cancel()
                return "cancel_requested" if cancel else "lease_lost"

    async def _complete(
        self, job: ClaimedJob, stage: Stage, ctx: StageContext, result: Any
    ) -> bool:
        async with self.sessionmaker() as session:
            if not await queue.mark_completed(session, job):
                await session.rollback()
                return False
            await bind_tenant(session, job.tenant_id)  # stage outputs live under RLS
            await stage.persist(session, ctx, result)
            following = next_stage(job.job_type)
            if following is not None:
                queue.enqueue(
                    session,
                    tenant_id=job.tenant_id,
                    document_id=job.document_id,
                    version_id=job.version_id,
                    job_type=following,
                    max_attempts=self.settings.job_max_attempts,
                )
            else:
                await queue.set_version_status(
                    session, job, DocumentStatus.PROCESSED, pipeline_version=PIPELINE_VERSION
                )
            await session.commit()
            return True

    async def _fail(
        self, job: ClaimedJob, code: str, message: str, *, permanent: bool
    ) -> JobStatus | None:
        delay = queue.backoff_seconds(
            job.attempt, self.settings.retry_base_seconds, self.settings.retry_max_seconds
        )
        async with self.sessionmaker() as session:
            return await queue.mark_failed(
                session,
                job,
                code=code,
                message=message,
                permanent=permanent,
                retry_in_seconds=delay,
            )
