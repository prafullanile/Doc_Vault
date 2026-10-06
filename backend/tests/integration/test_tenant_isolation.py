"""Tenant isolation, tested at two layers:

1. Through the API: Org B can never see or touch Org A's data.
2. Directly in PostgreSQL as the app role: RLS hides other tenants' rows even when a query
   has no tenant filter at all — the guarantee survives an application bug.
"""

import uuid

import asyncpg
import pytest

from tests.integration.helpers import auth, register, upload


@pytest.fixture
async def two_tenants(client):
    a = await register(client, org="Org A")
    b = await register(client, org="Org B")
    doc_id = (await upload(client, a["access_token"])).json()["document_id"]
    return a, b, doc_id


async def test_other_tenant_gets_404_not_403(client, two_tenants):
    _, b, doc_id = two_tenants
    token = auth(b["access_token"])
    # 404 (not 403) so B can't even learn that the ID exists.
    assert (await client.get(f"/v1/documents/{doc_id}", headers=token)).status_code == 404
    assert (await client.delete(f"/v1/documents/{doc_id}", headers=token)).status_code == 404
    rename = await client.patch(
        f"/v1/documents/{doc_id}", json={"filename": "pwned.pdf", "version": 1}, headers=token
    )
    assert rename.status_code == 404
    listing = await client.get("/v1/documents", headers=token)
    assert listing.json()["items"] == []


async def test_other_tenant_audit_log_is_invisible(client, two_tenants):
    _, b, doc_id = two_tenants
    logs = (
        await client.get("/v1/organizations/current/audit-logs", headers=auth(b["access_token"]))
    ).json()["items"]
    assert doc_id not in {item["target_id"] for item in logs}


async def _set_tenant(conn: asyncpg.Connection, tenant_id: str) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)


async def test_rls_hides_all_rows_when_no_tenant_is_set(two_tenants, app_conn, owner_conn):
    assert await owner_conn.fetchval("SELECT count(*) FROM documents") == 1  # the row exists
    assert await app_conn.fetchval("SELECT count(*) FROM documents") == 0
    assert await app_conn.fetchval("SELECT count(*) FROM audit_logs") == 0


async def test_rls_scopes_unfiltered_queries_to_the_current_tenant(two_tenants, app_conn):
    a, b, _ = two_tenants
    async with app_conn.transaction():
        await _set_tenant(app_conn, a["org_id"])
        assert await app_conn.fetchval("SELECT count(*) FROM documents") == 1
    async with app_conn.transaction():
        await _set_tenant(app_conn, b["org_id"])
        assert await app_conn.fetchval("SELECT count(*) FROM documents") == 0
    # The setting is transaction-local: nothing leaks into the next transaction.
    assert await app_conn.fetchval("SELECT count(*) FROM documents") == 0


async def test_rls_blocks_writing_rows_for_another_tenant(two_tenants, app_conn):
    a, b, doc_id = two_tenants
    async with app_conn.transaction():
        await _set_tenant(app_conn, b["org_id"])
        # Updating A's row from B's context silently matches nothing...
        result = await app_conn.execute(
            "UPDATE documents SET filename = 'pwned' WHERE id = $1", uuid.UUID(doc_id)
        )
        assert result == "UPDATE 0"
        # ...and inserting a row tagged with A's tenant violates the WITH CHECK policy.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(
                "INSERT INTO documents (id, tenant_id, filename) VALUES ($1, $2, 'x')",
                uuid.uuid4(),
                uuid.UUID(a["org_id"]),
            )


async def test_app_role_privileges_are_minimal(two_tenants, app_conn):
    a, _, _ = two_tenants
    async with app_conn.transaction():
        await _set_tenant(app_conn, a["org_id"])
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("DELETE FROM documents")  # soft delete only
    async with app_conn.transaction():
        await _set_tenant(app_conn, a["org_id"])
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("UPDATE audit_logs SET action = 'tampered'")  # append-only
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")
