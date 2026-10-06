# DocuNexus

A distributed, AI-powered document intelligence platform: large-scale document ingestion,
asynchronous processing, hybrid search and source-grounded question answering.
The full design is in [DocuNexus_Project_Context.md](DocuNexus_Project_Context.md).

**Status: Phase 1 (Backend Foundation).** It provides authentication, organizations and RBAC,
tenant isolation enforced by PostgreSQL row-level security, document upload and CRUD,
audit logs, and CI.

## Quickstart

```bash
cp .env.example .env          # optional; the defaults work locally
docker compose up --build     # postgres → migrate (one-shot) → api
```

- API: http://localhost:8000. Interactive docs: http://localhost:8000/docs
- Health: `GET /health/live`, `GET /health/ready`

```bash
# Register (creates you, your organization, and makes you ADMIN)
curl -s -X POST localhost:8000/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"correct-horse-battery","organization_name":"Acme"}'

# Upload a document
curl -s -X POST localhost:8000/v1/documents -H "Authorization: Bearer $TOKEN" -F file=@report.pdf
```

## API (v1)

| Area | Endpoints |
|---|---|
| Auth | `POST /v1/auth/register` · `login` · `refresh` · `logout` · `switch-org` · `GET /v1/auth/me` |
| Organization | `GET /v1/organizations/current` · `GET/POST /current/members` · `PATCH/DELETE /current/members/{user_id}` · `GET /current/audit-logs` |
| Documents | `POST /v1/documents` (multipart) · `GET /v1/documents` · `GET/PATCH/DELETE /v1/documents/{id}` |

**Roles.** All members can read. `ADMIN`, `ANALYST` and `USER` can upload and rename
documents. Only `ADMIN` can delete documents, manage members or read the audit log.

**Errors.** Every error, including 404, 422 and 500, uses one format:

```json
{"error": {"code": "DOCUMENT_NOT_FOUND", "message": "Document does not exist", "request_id": "req_…"}}
```

**Pagination.** Lists use keyset pagination: `?limit=20&cursor=<next_cursor>`, with
`sort=-created_at` (the default) or `sort=created_at`.

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

To run the API outside Docker:

1. Set `DATABASE_URL` (connecting as the app role) and `JWT_SECRET`.
2. Run `MIGRATIONS_DATABASE_URL=… uv run alembic upgrade head`, connecting as the owner role.
3. Start the server with `uv run uvicorn --factory app.main:create_app --reload`.

## Layout

```text
backend/
  app/
    api/            health checks, v1 router
    auth/           users, tokens, principal and RBAC dependencies
    organizations/  organizations, memberships, member management
    documents/      upload validation, storage backend, CRUD
    audit/          append-only audit log
    database/       engine, sessions, tenant context for RLS
    common/         error envelope, request-ID middleware, pagination
    core/           settings, logging
  migrations/       Alembic (run as the schema owner)
  tests/            unit/ and integration/ (real Postgres)
infrastructure/postgres/init/   creates the restricted app role
docs/adr/                       architecture decision records
```
