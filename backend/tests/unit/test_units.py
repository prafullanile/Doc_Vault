import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.common.errors import BadRequest, PayloadTooLarge, UnprocessableEntity, UnsupportedMediaType
from app.common.pagination import decode_cursor, encode_cursor
from app.core.config import Settings
from app.documents.validation import (
    DOCX,
    JPEG,
    PDF,
    PNG,
    TEXT,
    detect_file_type,
    digest_file,
    sanitize_filename,
)

SETTINGS = Settings(jwt_secret="unit-test-secret-" + "z" * 32)


# --- pagination ------------------------------------------------------------------------------


def test_cursor_round_trip():
    ts = datetime(2026, 10, 6, 12, 30, 45, 123456, tzinfo=UTC)
    id_ = uuid.uuid4()
    assert decode_cursor(encode_cursor(ts, id_)) == (ts, id_)


@pytest.mark.parametrize("cursor", ["", "!!!", "e30", "eyJjIjogIngiLCAiaSI6ICJ5In0"])
def test_invalid_cursor_is_a_400(cursor):
    with pytest.raises(BadRequest):
        decode_cursor(cursor)


# --- tokens and passwords ----------------------------------------------------------------------


def test_access_token_round_trip():
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(SETTINGS, user_id, org_id, "ADMIN")
    claims = decode_access_token(SETTINGS, token)
    assert (claims.user_id, claims.org_id, claims.role) == (user_id, org_id, "ADMIN")


def test_expired_and_wrong_type_tokens_are_rejected():
    now = datetime.now(UTC)
    base = {
        "iss": SETTINGS.jwt_issuer,
        "aud": SETTINGS.jwt_audience,
        "sub": str(uuid.uuid4()),
        "org": str(uuid.uuid4()),
        "role": "ADMIN",
        "iat": now,
    }
    expired = jwt.encode(
        {**base, "type": "access", "exp": now - timedelta(minutes=5)}, SETTINGS.jwt_secret
    )
    refresh_typed = jwt.encode(
        {**base, "type": "refresh", "exp": now + timedelta(minutes=5)}, SETTINGS.jwt_secret
    )
    wrong_audience = jwt.encode(
        {**base, "aud": "other", "type": "access", "exp": now + timedelta(minutes=5)},
        SETTINGS.jwt_secret,
    )
    for token in (expired, refresh_typed, wrong_audience):
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token(SETTINGS, token)


def test_none_algorithm_is_rejected():
    token = jwt.encode({"sub": "x"}, key=None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(SETTINGS, token)


async def test_password_hashing():
    hashed = await hash_password("correct-horse-battery")
    assert hashed.startswith("$argon2id$")
    assert await verify_password(hashed, "correct-horse-battery")
    assert not await verify_password(hashed, "wrong")
    assert not await verify_password("not-a-hash", "wrong")


def test_refresh_tokens_are_random_and_stored_hashed():
    raw1, hash1 = new_refresh_token()
    raw2, _ = new_refresh_token()
    assert raw1 != raw2
    assert hash1 == hash_refresh_token(raw1) != raw1
    assert len(hash1) == 64


# --- upload validation -------------------------------------------------------------------------


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"%PDF-1.7 ...", PDF),
        (b"\x89PNG\r\n\x1a\n....", PNG),
        (b"\xff\xd8\xff\xe0....", JPEG),
        (_docx(), DOCX),
        ("Plain UTF-8 text — with ünïcode".encode(), TEXT),
    ],
)
def test_detect_file_type(content, expected):
    assert detect_file_type(content[:8192], io.BytesIO(content)) == expected


def test_text_cut_mid_character_is_still_text():
    content = ("x" * 8191 + "é").encode()  # 'é' is 2 bytes; the head cuts it in half
    assert detect_file_type(content[:8192], io.BytesIO(content)) == TEXT


@pytest.mark.parametrize(
    "content", [b"MZ\x90\x00binary", b"\x00\x01\x02\x03", b"\xc3\x28 bad utf8"]
)
def test_unsupported_types(content):
    with pytest.raises(UnsupportedMediaType):
        detect_file_type(content, io.BytesIO(content))


def test_digest_file():
    digest = digest_file(io.BytesIO(b"hello"), max_bytes=10)
    assert digest.size_bytes == 5
    assert digest.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    with pytest.raises(PayloadTooLarge):
        digest_file(io.BytesIO(b"x" * 11), max_bytes=10)
    with pytest.raises(UnprocessableEntity):
        digest_file(io.BytesIO(b""), max_bytes=10)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\secret.docx", "secret.docx"),
        ("bad\x00name\x1f.pdf", "badname.pdf"),
        ("  ..  ", "document"),
        (None, "document"),
        ("a" * 300 + ".pdf", "a" * 251 + ".pdf"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected
