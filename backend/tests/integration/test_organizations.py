import asyncio

from tests.integration.helpers import add_member, auth, register, unique_email


async def test_get_current_org_and_members(client):
    admin = await register(client, org="Globex")
    org = await client.get("/v1/organizations/current", headers=auth(admin["access_token"]))
    assert org.status_code == 200
    assert org.json()["name"] == "Globex"
    assert org.json()["slug"].startswith("globex-")

    viewer = await add_member(client, admin["access_token"], "VIEWER")
    members = await client.get(
        "/v1/organizations/current/members", headers=auth(viewer["access_token"])
    )
    assert {m["role"] for m in members.json()} == {"ADMIN", "VIEWER"}


async def test_only_admins_manage_members(client):
    admin = await register(client)
    user = await add_member(client, admin["access_token"], "USER")
    resp = await client.post(
        "/v1/organizations/current/members",
        json={"email": unique_email(), "role": "ADMIN"},
        headers=auth(user["access_token"]),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_add_member_errors(client):
    admin = await register(client)
    unknown = await client.post(
        "/v1/organizations/current/members",
        json={"email": unique_email(), "role": "USER"},
        headers=auth(admin["access_token"]),
    )
    assert unknown.status_code == 404

    member = await add_member(client, admin["access_token"], "USER")
    again = await client.post(
        "/v1/organizations/current/members",
        json={"email": member["email"], "role": "USER"},
        headers=auth(admin["access_token"]),
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ALREADY_MEMBER"


async def test_role_change_takes_effect_immediately(client):
    """Authorization reads the membership per request, so an existing access token loses
    rights as soon as the role changes — not when the token expires."""
    admin = await register(client)
    analyst = await add_member(client, admin["access_token"], "ANALYST")
    me = await client.get("/v1/auth/me", headers=auth(analyst["access_token"]))
    assert me.json()["role"] == "ANALYST"

    await client.patch(
        f"/v1/organizations/current/members/{analyst['user_id']}",
        json={"role": "VIEWER"},
        headers=auth(admin["access_token"]),
    )
    upload = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=auth(analyst["access_token"]),
    )
    assert upload.status_code == 403


async def test_removed_member_loses_access_immediately(client):
    admin = await register(client)
    user = await add_member(client, admin["access_token"], "USER")
    resp = await client.delete(
        f"/v1/organizations/current/members/{user['user_id']}",
        headers=auth(admin["access_token"]),
    )
    assert resp.status_code == 204

    me = await client.get("/v1/auth/me", headers=auth(user["access_token"]))
    assert me.status_code == 401
    refresh = await client.post("/v1/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert refresh.status_code == 401


async def test_last_admin_cannot_be_removed_or_demoted(client):
    admin = await register(client)
    me = (await client.get("/v1/auth/me", headers=auth(admin["access_token"]))).json()
    user_id = me["user"]["id"]
    for method, kwargs in (("patch", {"json": {"role": "USER"}}), ("delete", {})):
        resp = await client.request(
            method.upper(),
            f"/v1/organizations/current/members/{user_id}",
            headers=auth(admin["access_token"]),
            **kwargs,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "LAST_ADMIN"


async def test_two_admins_demoting_each_other_concurrently_leaves_one_admin(client):
    admin_a = await register(client)
    admin_b = await add_member(client, admin_a["access_token"], "ADMIN")
    a_id = (await client.get("/v1/auth/me", headers=auth(admin_a["access_token"]))).json()["user"][
        "id"
    ]

    results = await asyncio.gather(
        client.patch(
            f"/v1/organizations/current/members/{admin_b['user_id']}",
            json={"role": "USER"},
            headers=auth(admin_a["access_token"]),
        ),
        client.patch(
            f"/v1/organizations/current/members/{a_id}",
            json={"role": "USER"},
            headers=auth(admin_b["access_token"]),
        ),
    )
    statuses = sorted(r.status_code for r in results)
    # The row locks serialise the two requests: one demotion wins. The loser either hits the
    # last-admin guard (409) or has already lost admin rights by the time it is checked (403).
    assert statuses[0] == 200
    assert statuses[1] in (403, 409)

    members = await client.get(
        "/v1/organizations/current/members", headers=auth(admin_a["access_token"])
    )
    if members.status_code == 200:
        roles = [m["role"] for m in members.json()]
    else:
        members = await client.get(
            "/v1/organizations/current/members", headers=auth(admin_b["access_token"])
        )
        roles = [m["role"] for m in members.json()]
    assert roles.count("ADMIN") == 1


async def test_audit_log_records_changes_and_is_admin_only(client):
    admin = await register(client)
    user = await add_member(client, admin["access_token"], "USER")
    logs = await client.get(
        "/v1/organizations/current/audit-logs", headers=auth(admin["access_token"])
    )
    assert logs.status_code == 200
    actions = [item["action"] for item in logs.json()["items"]]
    assert actions == ["member.added", "organization.created"]  # newest first
    assert logs.json()["items"][0]["request_id"]

    forbidden = await client.get(
        "/v1/organizations/current/audit-logs", headers=auth(user["access_token"])
    )
    assert forbidden.status_code == 403
