# ADR-0004: PostgreSQL is the source of truth for processing state

**Status:** Accepted. The queue is implemented in Phase 2 (ADR-0005); the outbox follows in Phase 3.

## Context
Uploading a document must create the document and its processing work atomically (§36). It
must also survive Kafka being unavailable (§45), retry with backoff (§29) and be
idempotent (§30).

## Decision
- Job state (status, attempts, leases, next attempt time) lives in PostgreSQL. Kafka, once
  added, carries notifications and never decides whether work is done.
- **Phase 2:** a Postgres job queue.
  - Workers claim jobs with `FOR UPDATE SKIP LOCKED` and a lease, and a reaper requeues jobs
    whose lease has expired.
  - Retries use capped exponential backoff with jitter.
  - Errors are classified as `RETRYABLE` or `PERMANENT`, so corrupt files fail immediately
    instead of filling the dead-letter queue.
- **Phase 3:** a transactional outbox. Events are written in the same transaction as the state
  change, and a relay publishes them to Kafka, giving at-least-once delivery.
- **Idempotency key per pipeline stage:** `(document_version_id, stage, pipeline_version)`.
  Each stage's output is replaced in a single transaction.

## Consequences
- Uploads stay consistent even when Kafka is down; events are published once it recovers.
- Switching from the Postgres queue to Kafka changes how workers are woken up, not job
  semantics.
- Phase 1 already applies the same principle on a small scale:
  - Object storage is written before the database row, so a crash leaves an orphan file (to
    be cleaned up later), never a row pointing at a missing file.
  - The `(tenant_id, checksum)` unique index, not the application's pre-check, is what
    guarantees no duplicate documents.
