# ADR-0002: Enforce tenant isolation in PostgreSQL with row-level security

**Status:** Accepted · Phase 1

## Context
A user from organization A must never see organization B's data (§11). If isolation depends
only on every query remembering `WHERE tenant_id = ?`, a single missed filter leaks data.

## Decision
- Every tenant-owned table has `tenant_id` and an RLS policy:
  `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`. The policy is used as
  both `USING` and `WITH CHECK`, with `FORCE ROW LEVEL SECURITY`.
- The API connects as `docunexus_app`, which is not the owner, not a superuser and has
  `NOBYPASSRLS`. Migrations run as `docunexus_owner`.
- A tenant-bound SQLAlchemy session sets `app.tenant_id` at the start of every transaction,
  using an `after_begin` hook. The setting is transaction-local, so a pooled connection never
  carries a tenant into the next request.
- Queries still filter by `tenant_id` explicitly. RLS is the backstop, not the only defence.
- The app role gets least privilege: no hard `DELETE` on documents (soft delete only), and
  only `INSERT`/`SELECT` on `audit_logs`, which makes them append-only.
- `users`, `organizations` and `memberships` are not under tenant RLS, because login must read
  memberships before a tenant is chosen. Every query on these tables filters on the caller's
  organization.
- Organization endpoints only operate on `/organizations/current`, so a client can never
  address another tenant's organization by ID.

## Consequences
- Tests check isolation at two layers:
  - Through the API: cross-tenant access returns 404.
  - As the raw app role, with no application code involved: zero rows are visible without a
    tenant, and cross-tenant inserts are rejected.
- Phase 4 caveat: HNSW vector search applies the RLS filter *after* the approximate scan.
  It will need `hnsw.iterative_scan` or partitioning by tenant.
