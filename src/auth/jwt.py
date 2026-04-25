"""JWT encode/decode using JWT_SECRET from settings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt

ALGORITHM = "HS256"
DEFAULT_TTL = timedelta(hours=12)


def encode(secret: str, payload: dict[str, Any], ttl: timedelta = DEFAULT_TTL) -> str:
    now = datetime.now(tz=timezone.utc)
    body = {**payload, "iat": int(now.timestamp()), "exp": int((now + ttl).timestamp())}
    return pyjwt.encode(body, secret, algorithm=ALGORITHM)


def decode(secret: str, token: str) -> dict[str, Any]:
    return pyjwt.decode(token, secret, algorithms=[ALGORITHM])
