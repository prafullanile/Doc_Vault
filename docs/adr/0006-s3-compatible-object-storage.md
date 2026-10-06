# ADR-0006: S3-compatible object storage, with RustFS for local development

**Status:** Accepted · Phase 2

## Context
The project plan (§6) calls for MinIO locally and S3 in production. While building Phase 2
we found that MinIO no longer publishes community container images: the Docker Hub
repository is gone and quay.io requires authentication. A local setup that cannot be pulled
breaks "reproducible with one command" (§41).

## Decision
- The application talks only to the S3 API (boto3), through a `StorageBackend` interface with
  two implementations:
  - `S3Storage`, used for Compose and production.
  - `LocalStorage`, used for tests and the simplest development setup.
- Docker Compose runs **RustFS** (`rustfs/rustfs:1.0.1`, Apache-2.0) as the S3-compatible
  server. It is a MinIO-compatible drop-in, and only the image changes.
- Object keys are `{tenant}/documents/{document}/versions/{version}/original.{ext}`, which
  keeps every tenant's objects under its own prefix.
- **Write order:** the object is written before the database row. A crash in between leaves
  an unreferenced object, never a row pointing at a missing file. A cleanup job for orphaned
  objects is future work.
- **Failure handling:**
  - Storage failures surface as `StorageError`. The API maps them to
    `503 STORAGE_UNAVAILABLE` with `Retry-After`, and the worker retries them with backoff.
  - A missing object is a permanent `FILE_MISSING`.
- Startup retries bucket creation for up to 60 seconds and tolerates another process creating
  the bucket first, since the API and the worker start together.

## Consequences
- The application code is not tied to any vendor. MinIO, RustFS, SeaweedFS, Garage and AWS S3
  are all configuration choices.
- Unit tests cover `S3Storage` against moto's S3 server, and CI's Compose smoke test covers it
  against RustFS.
- The development credentials are root credentials. Production should use a scoped key with
  access to only the one bucket.
