# DocuNexus

A distributed, AI-powered document intelligence platform: large-scale document ingestion,
asynchronous processing, hybrid search and source-grounded question answering.
The full design is in [DocuNexus_Project_Context.md](DocuNexus_Project_Context.md).

**Status: Phase 2 (Document Processing) complete.**

- **Phase 1:** authentication, organizations and RBAC, tenant isolation enforced by PostgreSQL
  row-level security, document CRUD, audit logs and CI.
- **Phase 2:**
  - S3-compatible object storage and document versions.
  - A PostgreSQL job queue with a background worker: leases, retries with backoff, a
    dead-letter state and cancellation.
  - Page-by-page text extraction for PDF, DOCX, TXT and images, with OCR on scanned pages.
  - Language detection.

## Quickstart

```bash
cp .env.example .env          # optional; the defaults work locally
docker compose up --build     # postgres, object-store → migrate (one-shot) → api + worker
./scripts/smoke_test.sh       # optional: uploads a text PDF and a scanned PDF end to end
```

- API: http://localhost:8000. Interactive docs: http://localhost:8000/docs
- Object store console (RustFS): http://localhost:9001
- Health: `GET /health/live`, `GET /health/ready`

```bash
# Register (creates you, your organization, and makes you ADMIN)
curl -s -X POST localhost:8000/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"correct-horse-battery","organization_name":"Acme"}'

# Upload: returns immediately with status QUEUED; the worker processes it in the background
curl -s -X POST localhost:8000/v1/documents -H "Authorization: Bearer $TOKEN" -F file=@report.pdf

# Watch it move QUEUED → PROCESSING → PROCESSED, then read the extracted pages
curl -s localhost:8000/v1/documents/$DOC/status -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/v1/documents/$DOC/pages  -H "Authorization: Bearer $TOKEN"
```

## API (v1)

| Area | Endpoints |
|---|---|
| Auth | `POST /v1/auth/register` · `login` · `refresh` · `logout` · `switch-org` · `GET /v1/auth/me` |
| Organization | `GET /v1/organizations/current` · `GET/POST /current/members` · `PATCH/DELETE /current/members/{user_id}` · `GET /current/audit-logs` |
| Documents | `POST /v1/documents` (multipart) · `GET /v1/documents` · `GET/PATCH/DELETE /v1/documents/{id}` |
| Versions | `POST /v1/documents/{id}/versions` (multipart) · `GET /v1/documents/{id}/versions` |
| Processing | `GET /v1/documents/{id}/status` · `POST /v1/documents/{id}/process` (reprocess) · `GET /v1/documents/{id}/pages` |
| Jobs | `GET /v1/jobs/{id}` (with attempt history) · `POST /v1/jobs/{id}/cancel` |

**Roles.**
- All members can read.
- `ADMIN`, `ANALYST` and `USER` can upload, add versions, rename, reprocess and cancel.
- Only `ADMIN` can delete documents, manage members or read the audit log.

**Errors.** Every error uses one format, including 404, 422, 500 and 503 (when the object
store is unreachable):

```json
{"error": {"code": "DOCUMENT_NOT_FOUND", "message": "Document does not exist", "request_id": "req_…"}}
```

**Pagination.**
- Document lists use keyset pagination: `?limit=20&cursor=<next_cursor>`, with
  `sort=-created_at` (the default) or `sort=created_at`.
- Pages use `?after=<next_cursor>`.

## How processing works

```text
POST /v1/documents ──► one transaction: document + version 1 + EXTRACT_TEXT job
                       (the file is written to object storage first)
worker ──► claims a job (FOR UPDATE SKIP LOCKED) and holds a lease, renewed by heartbeat
       ──► EXTRACT_TEXT: download → extract in a sandboxed subprocess → pages
       ──► completion transaction: save pages + mark job done + enqueue DETECT_LANGUAGE
       ──► DETECT_LANGUAGE → version and document marked PROCESSED
```

- **Bad files** (corrupt, encrypted, zip bombs) fail once, with a clear `error_code`.
- **Transient failures** (storage down, extractor crash) retry with exponential backoff and
  jitter, then move to `DEAD_LETTER`.
- **Crashed workers:** their leases expire and the reaper requeues the jobs.
- **Stalled workers:** fencing stops a worker that lost its lease from overwriting a newer
  attempt.

See ADR-0004 to ADR-0006 for the design.

## Development

```bash
cd backend
uv sync                       # Python 3.12 + all dependencies
uv run ruff check . && uv run ruff format --check . && uv run mypy app
uv run pytest                 # unit + integration tests
```

Integration tests run against a real PostgreSQL server, using the real migrations and RLS
policies. Set `TEST_POSTGRES_URL` to a server where that user can create databases and roles
(for example `postgresql://postgres:postgres@localhost:5432/postgres`). If it isn't set, the
tests start a container through testcontainers, which needs Docker.

- S3 storage tests use moto's in-process S3 server, so they need no Docker.
- OCR tests are skipped unless Tesseract is installed. CI installs it.

To run outside Docker:

1. Run `MIGRATIONS_DATABASE_URL=… uv run alembic upgrade head`, connecting as the owner role.
2. Start the API with `DATABASE_URL` set to the app role, plus `JWT_SECRET`:
   `uv run uvicorn --factory app.main:create_app --reload`.
3. Start the worker with `DATABASE_URL` set to the worker role: `uv run python -m app.worker`.

Storage defaults to local disk (`STORAGE_BACKEND=local`). Set `STORAGE_BACKEND=s3` and the
`S3_*` variables to use an S3-compatible server.

## Layout

```text
backend/
  app/
    api/            health checks, v1 router
    auth/           users, tokens, principal and RBAC dependencies
    organizations/  organizations, memberships, member management
    documents/      upload validation, storage backends, versions, pages, CRUD
    processing/     job queue, pipeline stages, jobs API
      extraction/   PDF/DOCX/TXT/image extractors, OCR, subprocess runner
    worker/         the worker process (python -m app.worker)
    audit/          append-only audit log
    database/       engine, sessions, tenant context for RLS
    common/         error envelope, request-ID middleware, pagination
    core/           settings, logging
  migrations/       Alembic (run as the schema owner)
  tests/            unit/ and integration/ (real Postgres)
infrastructure/postgres/init/   creates the restricted app and worker roles
scripts/                        end-to-end smoke test for the Compose stack
docs/adr/                       architecture decision records
```
