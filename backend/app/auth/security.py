import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

_hasher = PasswordHasher()


# Argon2 is deliberately slow (~tens of ms); run it off the event loop.
async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await to_thread.run_sync(_verify)


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


async def burn_password_check(password: str) -> None:
    """Spend the same time as a real check, so unknown emails can't be detected by timing."""
    await verify_password(_dummy_hash(), password)


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str


def create_access_token(
    settings: Settings, user_id: uuid.UUID, org_id: uuid.UUID, role: str
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "org": str(org_id),
        "role": role,  # informational for clients; authorization re-reads the membership
        "type": "access",
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """Raises jwt.InvalidTokenError on any problem (signature, expiry, audience, shape)."""
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        leeway=10,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    try:
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            org_id=uuid.UUID(payload["org"]),
            role=str(payload["role"]),
        )
    except (KeyError, ValueError) as exc:
        raise jwt.InvalidTokenError("malformed claims") from exc


def new_refresh_token() -> tuple[str, str]:
    """Returns (token for the client, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    # A fast hash is fine here: the token is 256 random bits, not a guessable password.
    return hashlib.sha256(token.encode()).hexdigest()
