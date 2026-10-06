"""The processing pipeline end to end: upload → queue → worker → pages, plus the failure modes
from §45: worker crash, transient outage, corrupt input, duplicate claims, cancellation."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.documents.storage import StorageError
from app.processing import queue
from app.processing.extraction.ocr import tesseract_available
from app.processing.models import JobType
from app.processing.stages import STAGES, StageContext
from app.worker.worker import Worker
from tests import factories
from tests.integration.helpers import auth, register

needs_tesseract = pytest.mark.skipif(not tesseract_available(), reason="Tesseract not installed")


async def upload_file(client, token, content: bytes, filename: str, mime: str) -> dict[str, Any]:
    resp = await client.post(
        "/v1/documents", files={"file": (filename, content, mime)}, headers=auth(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def upload_pdf(client, token, pages: list[str] | None = None) -> dict[str, Any]:
    content = factories.text_pdf(pages or [factories.SAMPLE_ENGLISH, "Second page. " * 5])
    return await upload_file(client, token, content, "report.pdf", "application/pdf")


async def upload_pdf_raw(client, token):
    return await client.post(
        "/v1/documents",
        files={"file": ("r.pdf", factories.text_pdf(["text"]), "application/pdf")},
        headers=auth(token),
    )


async def drain(worker: Worker) -> int:
    """Runs jobs until the queue has nothing claimable."""
    processed = 0
    while await worker.run_once():
        processed += 1
    return processed


async def status_of(client, token, document_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/documents/{document_id}/status", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def make_due(owner_conn) -> None:
    """Skips the backoff wait for every retrying job."""
    await owner_conn.execute(
        "UPDATE processing_jobs SET next_attempt_at = now() WHERE status = 'RETRYING'"
    )


# --- happy path --------------------------------------------------------------------------------


async def test_upload_creates_version_and_queued_job(client):
    admin = await register(client)
    up = await upload_pdf(client, admin["access_token"])
    assert up["version_no"] == 1
    status = await status_of(client, admin["access_token"], up["document_id"])
    assert status["status"] == "QUEUED"
    assert [(j["job_type"], j["status"]) for j in status["jobs"]] == [("EXTRACT_TEXT", "QUEUED")]
    assert status["jobs"][0]["id"] == up["job_id"]


async def test_pdf_is_extracted_page_by_page(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token, [factories.SAMPLE_ENGLISH, "", "Third page text here."])

    assert await drain(worker) == 2  # EXTRACT_TEXT, then DETECT_LANGUAGE

    status = await status_of(client, token, up["document_id"])
    assert status["status"] == "PROCESSED"
    version = status["current_version"]
    assert version["page_count"] == 3
    assert version["ocr_page_count"] == 0
    assert version["language"] == "en"
    assert version["pipeline_version"]
    assert [(j["job_type"], j["status"], j["attempt"]) for j in status["jobs"]] == [
        ("EXTRACT_TEXT", "COMPLETED", 1),
        ("DETECT_LANGUAGE", "COMPLETED", 1),
    ]

    pages = (
        await client.get(f"/v1/documents/{up['document_id']}/pages", headers=auth(token))
    ).json()
    assert [(p["page_number"], p["source"]) for p in pages["items"]] == [
        (1, "TEXT"),
        (2, "EMPTY"),
        (3, "TEXT"),
    ]
    assert "Revenue increased" in pages["items"][0]["text"]

    doc = (await client.get(f"/v1/documents/{up['document_id']}", headers=auth(token))).json()
    assert (doc["status"], doc["page_count"], doc["language"]) == ("PROCESSED", 3, "en")


async def test_pages_are_paginated(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token, [f"Page {i} " * 10 for i in range(1, 6)])
    await drain(worker)
    url = f"/v1/documents/{up['document_id']}/pages"
    first = (await client.get(url, params={"limit": 2}, headers=auth(token))).json()
    assert [p["page_number"] for p in first["items"]] == [1, 2]
    rest = await client.get(
        url, params={"limit": 10, "after": first["next_cursor"]}, headers=auth(token)
    )
    assert [p["page_number"] for p in rest.json()["items"]] == [3, 4, 5]
    assert rest.json()["next_cursor"] is None


async def test_docx_is_split_by_heading(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    content = factories.docx_file(
        [
            ("Introduction", "This agreement is made between two parties."),
            ("Payment terms", "The buyer pays one hundred thousand dollars."),
        ],
        table=[["Item", "Amount"], ["Licence", "100000"]],
    )
    up = await upload_file(client, token, content, "contract.docx", factories.DOCX_MIME)
    await drain(worker)
    pages = (
        await client.get(f"/v1/documents/{up['document_id']}/pages", headers=auth(token))
    ).json()["items"]
    assert [p["section"] for p in pages] == ["Introduction", "Payment terms"]
    assert "Licence | 100000" in pages[1]["text"]


async def test_plain_text(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    content = ("Le chiffre d'affaires a augmenté de vingt pour cent cette année. " * 5).encode()
    up = await upload_file(client, token, content, "notes.txt", "text/plain")
    await drain(worker)
    status = await status_of(client, token, up["document_id"])
    assert status["status"] == "PROCESSED"
    assert status["current_version"]["language"] == "fr"


@needs_tesseract
async def test_scanned_pdf_is_ocrd(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    content = factories.scanned_pdf("Revenue increased\nby 23 percent")
    up = await upload_file(client, token, content, "scan.pdf", "application/pdf")
    await drain(worker)
    status = await status_of(client, token, up["document_id"])
    assert status["status"] == "PROCESSED", status
    assert status["current_version"]["ocr_page_count"] == 1
    page = (
        await client.get(f"/v1/documents/{up['document_id']}/pages", headers=auth(token))
    ).json()["items"][0]
    assert page["source"] == "OCR"
    assert "Revenue" in page["text"]
    assert page["ocr_confidence"] > 0.5


# --- failures ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"%PDF-1.7\n% not really a pdf\n%%EOF\n", "CORRUPT_FILE"),
        (factories.encrypted_pdf(), "ENCRYPTED_PDF"),
    ],
)
async def test_bad_files_fail_once_without_retrying(client, worker, content, expected_code):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_file(client, token, content, "bad.pdf", "application/pdf")

    assert await drain(worker) == 1
    status = await status_of(client, token, up["document_id"])
    assert status["status"] == "FAILED"
    assert status["current_version"]["error_code"] == expected_code
    (job,) = status["jobs"]
    assert (job["status"], job["attempt"], job["error_code"]) == ("FAILED", 1, expected_code)

    detail = (await client.get(f"/v1/jobs/{job['id']}", headers=auth(token))).json()
    assert [a["outcome"] for a in detail["attempts"]] == ["PERMANENT_ERROR"]


async def test_zip_bomb_docx_is_rejected(client, worker, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_decompressed_bytes", 200_000)
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_file(
        client, token, factories.zip_bomb_docx(5_000_000), "bomb.docx", factories.DOCX_MIME
    )
    await drain(worker)
    status = await status_of(client, token, up["document_id"])
    assert status["current_version"]["error_code"] == "DECOMPRESSION_LIMIT"


class FlakyStorage:
    """Delegates to real storage, but downloads fail while `failing` is set."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.failing = True

    async def download(self, key: str, destination: Path) -> None:
        if self.failing:
            raise StorageError("connection refused (simulated outage)")
        await self.inner.download(key, destination)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_transient_failures_back_off_then_dead_letter_then_recover(
    client, app, settings, worker_sessionmaker, owner_conn
):
    storage = FlakyStorage(app.state.storage)
    flaky_worker = Worker(settings, worker_sessionmaker, storage, worker_id="flaky")
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)

    # Attempt 1 fails: the job waits with backoff, the document is still "in progress".
    assert await flaky_worker.run_once()
    status = await status_of(client, token, up["document_id"])
    job = status["jobs"][0]
    assert (job["status"], job["attempt"], job["error_code"]) == ("RETRYING", 1, "STORAGEERROR")
    assert status["status"] == "PROCESSING"
    # Not due yet: nothing to claim.
    assert await flaky_worker.run_once() is None

    # Attempts 2 and 3 (max_attempts=3 in test settings) → dead letter.
    for _ in range(2):
        await make_due(owner_conn)
        assert await flaky_worker.run_once()
    status = await status_of(client, token, up["document_id"])
    assert status["jobs"][0]["status"] == "DEAD_LETTER"
    assert status["status"] == "FAILED"
    detail = (await client.get(f"/v1/jobs/{job['id']}", headers=auth(token))).json()
    assert [a["outcome"] for a in detail["attempts"]] == ["RETRYABLE_ERROR"] * 3

    # The dependency recovers; an operator reprocesses the document.
    storage.failing = False
    resp = await client.post(f"/v1/documents/{up['document_id']}/process", headers=auth(token))
    assert resp.status_code == 202
    await drain(flaky_worker)
    assert (await status_of(client, token, up["document_id"]))["status"] == "PROCESSED"


class BrokenStorage(FlakyStorage):
    async def put(self, key: str, data: Any) -> None:
        raise StorageError("object store unreachable (simulated)")


async def test_upload_during_storage_outage_is_503_and_writes_nothing(client, app, owner_conn):
    admin = await register(client)
    real = app.state.storage
    app.state.storage = BrokenStorage(real)
    try:
        resp = await upload_pdf_raw(client, admin["access_token"])
    finally:
        app.state.storage = real
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    assert resp.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    for table in ("documents", "document_versions", "processing_jobs"):
        assert await owner_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


def test_backoff_grows_exponentially_with_jitter_and_a_cap():
    assert queue.backoff_seconds(1, base=10, cap=600, rand=0) == 5
    assert queue.backoff_seconds(1, base=10, cap=600, rand=1) == 10
    assert queue.backoff_seconds(3, base=10, cap=600, rand=1) == 40
    assert queue.backoff_seconds(20, base=10, cap=600, rand=1) == 600


# --- crash recovery, fencing, concurrency ------------------------------------------------------


async def test_crashed_worker_job_is_reaped_and_reprocessed_without_duplicates(
    client, worker, worker_sessionmaker, owner_conn
):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)

    # A worker claims the job and dies without finishing.
    async with worker_sessionmaker() as session:
        crashed = await queue.claim(session, "doomed-worker", lease_seconds=60)
    assert crashed is not None
    await owner_conn.execute("UPDATE processing_jobs SET locked_until = now() - interval '1s'")

    assert await worker.reap() == 1
    status = await status_of(client, token, up["document_id"])
    assert status["jobs"][0]["status"] == "RETRYING"
    assert status["jobs"][0]["error_code"] == "LEASE_EXPIRED"

    await drain(worker)
    status = await status_of(client, token, up["document_id"])
    assert status["status"] == "PROCESSED"
    detail = (await client.get(f"/v1/jobs/{status['jobs'][0]['id']}", headers=auth(token))).json()
    assert [(a["attempt"], a["outcome"]) for a in detail["attempts"]] == [
        (1, "LEASE_EXPIRED"),
        (2, "SUCCEEDED"),
    ]
    pages = await owner_conn.fetchval("SELECT count(*) FROM document_pages")
    assert pages == status["current_version"]["page_count"] == 2


async def test_stale_worker_cannot_overwrite_newer_attempt(client, worker_sessionmaker, owner_conn):
    """Fencing: a worker that stalled past its lease can't complete the job afterwards."""
    admin = await register(client)
    await upload_pdf(client, admin["access_token"])

    async with worker_sessionmaker() as session:
        stale = await queue.claim(session, "slow-worker", lease_seconds=60)
    await owner_conn.execute("UPDATE processing_jobs SET locked_until = now() - interval '1s'")
    async with worker_sessionmaker() as session:
        await queue.reap_expired(session)
    await owner_conn.execute("UPDATE processing_jobs SET next_attempt_at = now()")
    async with worker_sessionmaker() as session:
        fresh = await queue.claim(session, "fast-worker", lease_seconds=60)
    assert stale and fresh and fresh.attempt == stale.attempt + 1

    async with worker_sessionmaker() as session:
        assert await queue.mark_completed(session, stale) is False
        await session.rollback()
    async with worker_sessionmaker() as session:
        assert await queue.mark_completed(session, fresh) is True
        await session.commit()


async def test_concurrent_workers_never_claim_the_same_job(client, worker_sessionmaker):
    admin = await register(client)
    for i in range(3):
        await upload_pdf(client, admin["access_token"], [f"Document number {i} " * 5])

    async def claim(n: int) -> queue.ClaimedJob | None:
        async with worker_sessionmaker() as session:
            return await queue.claim(session, f"worker-{n}", lease_seconds=60)

    results = await asyncio.gather(*(claim(n) for n in range(8)))
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 3
    assert len({c.id for c in claimed}) == 3


async def test_worker_run_loop_processes_everything_and_stops_gracefully(
    client, app, settings, worker_sessionmaker
):
    fast = settings.model_copy(
        update={"worker_poll_interval_seconds": 0.05, "worker_concurrency": 3}
    )
    loop_worker = Worker(fast, worker_sessionmaker, app.state.storage, worker_id="loop")
    admin = await register(client)
    token = admin["access_token"]
    ids = [
        (await upload_pdf(client, token, [f"Document {i}. " + factories.SAMPLE_ENGLISH]))[
            "document_id"
        ]
        for i in range(4)
    ]

    runner = asyncio.create_task(loop_worker.run())
    try:
        for _ in range(300):
            statuses = [(await status_of(client, token, i))["status"] for i in ids]
            if all(s == "PROCESSED" for s in statuses):
                break
            await asyncio.sleep(0.1)
        assert statuses == ["PROCESSED"] * 4
    finally:
        loop_worker.request_stop()
        await asyncio.wait_for(runner, timeout=30)


# --- cancellation and reprocessing -------------------------------------------------------------


async def test_cancel_queued_job(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)
    resp = await client.post(f"/v1/jobs/{up['job_id']}/cancel", headers=auth(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert await worker.run_once() is None
    status = await status_of(client, token, up["document_id"])
    assert (status["status"], status["current_version"]["error_code"]) == ("FAILED", "CANCELLED")

    again = await client.post(f"/v1/jobs/{up['job_id']}/cancel", headers=auth(token))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "JOB_NOT_ACTIVE"


class BlockingStage:
    """Stands in for a long extraction: runs until cancelled."""

    job_type = JobType.EXTRACT_TEXT

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, ctx: StageContext) -> None:
        self.started.set()
        await asyncio.sleep(3600)

    async def persist(self, session: Any, ctx: StageContext, result: Any) -> None:
        raise AssertionError("must not persist a cancelled job")


async def test_cancel_running_job_stops_the_worker_stage(
    client, app, settings, worker_sessionmaker
):
    stage = BlockingStage()
    quick = settings.model_copy(update={"job_lease_seconds": 1})
    blocking_worker = Worker(
        quick,
        worker_sessionmaker,
        app.state.storage,
        worker_id="blocking",
        stages={**STAGES, JobType.EXTRACT_TEXT: stage},
    )
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)

    running = asyncio.create_task(blocking_worker.run_once())
    await asyncio.wait_for(stage.started.wait(), timeout=10)
    resp = await client.post(f"/v1/jobs/{up['job_id']}/cancel", headers=auth(token))
    assert resp.json()["cancel_requested"] is True
    await asyncio.wait_for(running, timeout=10)  # stopped at the next heartbeat

    detail = (await client.get(f"/v1/jobs/{up['job_id']}", headers=auth(token))).json()
    assert detail["status"] == "CANCELLED"
    assert [a["outcome"] for a in detail["attempts"]] == ["CANCELLED"]


async def test_reprocess_rules(client, worker, owner_conn):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)
    url = f"/v1/documents/{up['document_id']}/process"

    busy = await client.post(url, headers=auth(token))
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "PROCESSING_IN_PROGRESS"

    await drain(worker)
    resp = await client.post(url, headers=auth(token))
    assert resp.status_code == 202
    assert resp.json()["status"] == "QUEUED"
    await drain(worker)

    status = await status_of(client, token, up["document_id"])
    assert status["status"] == "PROCESSED"
    assert [j["job_type"] for j in status["jobs"]] == ["EXTRACT_TEXT", "DETECT_LANGUAGE"] * 2
    # Re-running replaced the pages instead of adding a second copy.
    assert await owner_conn.fetchval("SELECT count(*) FROM document_pages") == 2


async def test_concurrent_reprocess_requests_create_one_job(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)
    await drain(worker)
    url = f"/v1/documents/{up['document_id']}/process"
    responses = await asyncio.gather(*(client.post(url, headers=auth(token)) for _ in range(4)))
    assert sorted(r.status_code for r in responses) == [202, 409, 409, 409]


# --- versions ----------------------------------------------------------------------------------


async def test_new_version_is_processed_and_old_pages_remain(client, worker):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token, ["Payment: 100000 dollars. " * 3])
    await drain(worker)

    resp = await client.post(
        f"/v1/documents/{up['document_id']}/versions",
        files={
            "file": (
                "v2.pdf",
                factories.text_pdf(["Payment: 125000 dollars. " * 3]),
                "application/pdf",
            )
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version_no"] == 2
    await drain(worker)

    doc = (await client.get(f"/v1/documents/{up['document_id']}", headers=auth(token))).json()
    assert (doc["current_version_no"], doc["status"]) == (2, "PROCESSED")
    versions = (
        await client.get(f"/v1/documents/{up['document_id']}/versions", headers=auth(token))
    ).json()
    assert [(v["version_no"], v["status"]) for v in versions] == [
        (1, "PROCESSED"),
        (2, "PROCESSED"),
    ]

    pages_url = f"/v1/documents/{up['document_id']}/pages"
    latest = (await client.get(pages_url, headers=auth(token))).json()
    v1 = (await client.get(pages_url, params={"version_no": 1}, headers=auth(token))).json()
    assert "125000" in latest["items"][0]["text"]
    assert "100000" in v1["items"][0]["text"]
    missing = await client.get(pages_url, params={"version_no": 9}, headers=auth(token))
    assert missing.status_code == 404


async def test_uploading_an_existing_file_as_new_version_is_a_duplicate(client):
    admin = await register(client)
    token = admin["access_token"]
    content = factories.text_pdf(["Same content " * 5])
    up = await upload_file(client, token, content, "a.pdf", "application/pdf")
    resp = await client.post(
        f"/v1/documents/{up['document_id']}/versions",
        files={"file": ("a.pdf", content, "application/pdf")},
        headers=auth(token),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_DOCUMENT"


async def test_delete_cancels_pending_processing(client, worker, owner_conn):
    admin = await register(client)
    token = admin["access_token"]
    up = await upload_pdf(client, token)
    assert (
        await client.delete(f"/v1/documents/{up['document_id']}", headers=auth(token))
    ).status_code == 204
    assert await owner_conn.fetchval("SELECT status FROM processing_jobs") == "CANCELLED"
    assert await worker.run_once() is None


# --- isolation ---------------------------------------------------------------------------------


async def test_jobs_pages_and_versions_are_tenant_isolated(client, worker):
    a = await register(client)
    b = await register(client)
    up = await upload_pdf(client, a["access_token"])
    await drain(worker)
    token_b = auth(b["access_token"])
    doc = up["document_id"]
    for method, url in [
        ("GET", f"/v1/jobs/{up['job_id']}"),
        ("POST", f"/v1/jobs/{up['job_id']}/cancel"),
        ("GET", f"/v1/documents/{doc}/status"),
        ("GET", f"/v1/documents/{doc}/pages"),
        ("GET", f"/v1/documents/{doc}/versions"),
        ("POST", f"/v1/documents/{doc}/process"),
    ]:
        resp = await client.request(method, url, headers=token_b)
        assert resp.status_code == 404, (method, url, resp.status_code)


async def test_worker_role_sees_the_queue_but_not_documents_without_a_tenant(
    client, worker, worker_conn, app_conn
):
    a = await register(client)
    await upload_pdf(client, a["access_token"])
    await drain(worker)
    # The worker must see every tenant's jobs to claim them...
    assert await worker_conn.fetchval("SELECT count(*) FROM processing_jobs") == 2
    # ...but document data stays behind RLS until it binds the job's tenant.
    for table in ("documents", "document_versions", "document_pages"):
        assert await worker_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    async with worker_conn.transaction():
        await worker_conn.execute("SELECT set_config('app.tenant_id', $1, true)", a["org_id"])
        assert await worker_conn.fetchval("SELECT count(*) FROM document_pages") == 2
    # The API role gets no such exception for the queue.
    assert await app_conn.fetchval("SELECT count(*) FROM processing_jobs") == 0
    assert await app_conn.fetchval("SELECT count(*) FROM document_pages") == 0


async def test_unknown_job_is_404(client):
    admin = await register(client)
    resp = await client.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth(admin["access_token"]))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"
