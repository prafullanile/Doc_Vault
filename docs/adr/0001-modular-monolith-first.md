# ADR-0001: Start as a modular monolith

**Status:** Accepted · Phase 1

## Context
The target architecture (§9 of the project context) has separate document, search, auth and
worker services behind an API gateway. Building those services on day one means paying for
network boundaries, distributed transactions and deployment complexity before the domain is
understood.

## Decision
DocuNexus starts as one FastAPI application, split into domain packages (`auth`,
`organizations`, `documents`, `audit`). Packages talk to each other only through service
functions, never through each other's tables. The "API gateway" concerns (request IDs,
authentication, tenant context, security headers) are middleware and dependencies inside the
application.

## Consequences
- One deployable unit, and transactions can span modules. Registration, for example, creates
  the user, the organization and the membership atomically.
- Services can be extracted later along the package boundaries. Workers (Phases 2–3) will be
  the first ones, running from the same codebase with a different entrypoint.
- A real gateway (Traefik, Kong or similar) only arrives once there is more than one service
  to route to.
