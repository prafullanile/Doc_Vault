"""Test helpers shared by integration tests."""

import uuid

import httpx


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


PASSWORD = "correct-horse-battery"


async def register(
    client: httpx.AsyncClient, email: str | None = None, org: str = "Acme Corp"
) -> dict[str, str]:
    email = email or unique_email()
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": PASSWORD, "organization_name": org},
    )
    assert resp.status_code == 201, resp.text
    return {**resp.json(), "email": email}


async def add_member(client: httpx.AsyncClient, admin_token: str, role: str) -> dict[str, str]:
    """Registers a user (in their own org), adds them to the admin's org with `role`, and
    returns tokens scoped to the admin's org."""
    member = await register(client, org="Personal")
    org_id = (await client.get("/v1/auth/me", headers=auth(admin_token))).json()["active_org_id"]
    resp = await client.post(
        "/v1/organizations/current/members",
        json={"email": member["email"], "role": role},
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/v1/auth/login", json={"email": member["email"], "password": PASSWORD, "org_id": org_id}
    )
    assert login.status_code == 200, login.text
    return {**login.json(), "email": member["email"], "user_id": resp.json()["user_id"]}


def pdf_bytes(marker: str | None = None) -> bytes:
    return b"%PDF-1.7\n% " + (marker or uuid.uuid4().hex).encode() + b"\n%%EOF\n"


async def upload(
    client: httpx.AsyncClient,
    token: str,
    content: bytes | None = None,
    filename: str = "report.pdf",
) -> httpx.Response:
    return await client.post(
        "/v1/documents",
        files={"file": (filename, pdf_bytes() if content is None else content, "application/pdf")},
        headers=auth(token),
    )
