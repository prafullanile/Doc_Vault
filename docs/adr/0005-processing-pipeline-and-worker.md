# ADR-0005: The processing pipeline and worker

**Status:** Accepted · Phase 2

## Context
ADR-0004 makes PostgreSQL the source of truth for job state. This ADR records how the
worker uses that queue, and how it stays correct when workers crash, stall, run in parallel,
or receive hostile files.

## Decision

**Queue and leases**
- `processing_jobs` has one row per pipeline stage per document version.
- Workers claim a job with `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`
  and receive a lease (`locked_until`).
- While a stage runs, the worker heartbeats every `lease / 3`.
- Each worker also runs a reaper, which requeues `RUNNING` jobs whose lease has expired.

**Fencing**
- Every completion, failure or cancellation is an `UPDATE` that also checks
  `status = 'RUNNING' AND locked_by = me AND attempt = n`.
- A worker that stalled past its lease, while the job was re-run elsewhere, matches zero rows,
  and its results are discarded.

**Stages**
- Each stage splits into `execute` (slow work, no database writes) and `persist`.
- `persist` runs inside the completion transaction, together with marking the job done and
  enqueueing the next stage, so the step is atomic.
- Outputs are replaced, never appended, so re-running a stage is idempotent.
- A partial unique index allows only one live job per `(version, stage)`.

**Failure classification**
- `PermanentError` (corrupt or encrypted file, zip bomb, missing OCR, extraction timeout)
  → `FAILED` after one attempt.
- Anything else → `RETRYING`, with capped exponential backoff and jitter.
- After `max_attempts` → `DEAD_LETTER`.
- Every attempt is recorded in `job_attempts`.

**Sandboxing**
- Extraction runs in a child process (`python -m app.processing.extraction.cli`) with a hard
  timeout. On POSIX it also has an address-space limit.
- A malicious file can only kill the child process. A crash is retryable; a timeout is
  permanent.

**Worker database role**
- The worker connects as `docunexus_worker`. A role-specific permissive policy lets it see
  every tenant's rows in `processing_jobs` and `job_attempts`; it needs this to claim work.
- Document tables keep plain tenant RLS for the worker, so it must bind the job's tenant before
  it reads or writes documents, versions or pages.
- The API role never sees other tenants' jobs.

**OCR per page.** A PDF page is OCR'd only when its text layer has fewer than
`ocr_min_chars` characters *and* the page contains images. Mixed PDFs with typed pages and
scanned annexes work.

## Consequences
- Processing scales by running more worker processes; `SKIP LOCKED` means they don't contend
  for the same jobs.
- Polling adds up to `worker_poll_interval_seconds` of latency. Phase 3's Kafka notifications
  (via the outbox) remove it without changing job semantics.
- Each attempt costs a few small writes (claim, heartbeats, completion), which is fine at this
  scale. Throughput limits will be measured in the load-testing phase.
