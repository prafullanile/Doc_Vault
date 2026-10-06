"""Keyset (cursor) pagination. Unlike OFFSET, cost stays constant however deep the client pages,
and rows inserted while paging don't cause duplicates or gaps."""

import base64
import binascii
import json
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.common.errors import BadRequest


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def encode_cursor(created_at: datetime, id_: uuid.UUID) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": str(id_)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(data["c"]), uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise BadRequest("Invalid pagination cursor", code="INVALID_CURSOR") from exc
