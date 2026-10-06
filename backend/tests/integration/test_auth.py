import asyncio

import jwt

from tests.integration.helpers import PASSWORD, auth, register, unique_email


async def test_register_returns_tokens_and_admin_role(client, settings):
    tokens = await register(client)
    assert tokens["role"] == "ADMIN"
    assert tokens["token_type"] == "bearer"
    claims = jwt.decode(
        tokens["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
    )
    assert claims["org"] == tokens["org_id"]
    assert claims["type"] == "access"


async def test_register_duplicate_email_conflicts_case_insensitively(client):
    email = unique_email()
    await register(client, email=email)
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email.upper(), "password": PASSWORD, "organization_name": "Other"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_concurrent_registrations_with_same_email(client):
    email = unique_email()
    body = {"email": email, "password": PASSWORD, "organization_name": "Race"}
    responses = await asyncio.gather(
        *(client.post("/v1/auth/register", json=body) for _ in range(3))
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409]


async def test_password_policy(client):
    resp = await client.post(
        "/v1/auth/register",
        json={"email": unique_email(), "password": "short", "organization_name": "Acme"},
    )
    assert resp.status_code == 422


async def test_login_success_and_failures_are_indistinguishable(client):
    tokens = await register(client)
    ok = await client.post("/v1/auth/login", json={"email": tokens["email"], "password": PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["org_id"] == tokens["org_id"]

    wrong_password = await client.post(
        "/v1/auth/login", json={"email": tokens["email"], "password": "wrong-password-123"}
    )
    unknown_email = await client.post(
        "/v1/auth/login", json={"email": unique_email(), "password": PASSWORD}
    )
    for resp in (wrong_password, unknown_email):
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert wrong_password.json()["error"]["message"] == unknown_email.json()["error"]["message"]


async def test_protected_route_requires_valid_token(client):
    missing = await client.get("/v1/auth/me")
    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"

    garbage = await client.get("/v1/auth/me", headers=auth("not.a.jwt"))
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "INVALID_TOKEN"


async def test_token_signed_with_other_secret_is_rejected(client, settings):
    tokens = await register(client)
    claims = jwt.decode(
        tokens["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
    )
    forged = jwt.encode({**claims, "role": "ADMIN"}, "attacker-secret-" + "y" * 40)
    resp = await client.get("/v1/auth/me", headers=auth(forged))
    assert resp.status_code == 401


async def test_me(client):
    tokens = await register(client, org="Initech")
    resp = await client.get("/v1/auth/me", headers=auth(tokens["access_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == tokens["email"]
    assert body["role"] == "ADMIN"
    assert [m["org_name"] for m in body["memberships"]] == ["Initech"]


async def test_refresh_rotates_token(client):
    tokens = await register(client)
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    rotated = resp.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    me = await client.get("/v1/auth/me", headers=auth(rotated["access_token"]))
    assert me.status_code == 200


async def test_refresh_token_reuse_revokes_whole_family(client):
    tokens = await register(client)
    first = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    new_refresh = first.json()["refresh_token"]

    # The old token is replayed (e.g. it was stolen) → reuse detected.
    replay = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"

    # ...and the legitimate descendant is dead too.
    after = await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert after.status_code == 401


async def test_concurrent_refresh_of_same_token_rotates_once(client):
    tokens = await register(client)
    body = {"refresh_token": tokens["refresh_token"]}
    responses = await asyncio.gather(
        *(client.post("/v1/auth/refresh", json=body) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [200, 401]


async def test_logout_revokes_refresh_token(client):
    tokens = await register(client)
    out = await client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 204
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401
    # Idempotent.
    again = await client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 204


async def test_switch_org(client):
    alice = await register(client, org="Alice Org")
    bob = await register(client, org="Bob Org")
    await client.post(
        "/v1/organizations/current/members",
        json={"email": alice["email"], "role": "VIEWER"},
        headers=auth(bob["access_token"]),
    )
    switched = await client.post(
        "/v1/auth/switch-org",
        json={"org_id": bob["org_id"]},
        headers=auth(alice["access_token"]),
    )
    assert switched.status_code == 200
    assert switched.json()["role"] == "VIEWER"
    org = await client.get(
        "/v1/organizations/current", headers=auth(switched.json()["access_token"])
    )
    assert org.json()["name"] == "Bob Org"


async def test_switch_to_foreign_org_is_forbidden(client):
    alice = await register(client)
    bob = await register(client)
    resp = await client.post(
        "/v1/auth/switch-org",
        json={"org_id": bob["org_id"]},
        headers=auth(alice["access_token"]),
    )
    assert resp.status_code == 403
