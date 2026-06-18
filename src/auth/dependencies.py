"""FastAPI dependencies for auth and RBAC."""

from __future__ import annotations

from typing import Callable

from fastapi import Cookie, Depends, HTTPException, status
import jwt as pyjwt

from src.auth import jwt as app_jwt
from src.config import ANONYMOUS_ADMIN, AppConfig, get_config

COOKIE_NAME = "optv_token"

ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


def current_user(
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    config: AppConfig = Depends(get_config),
) -> dict:
    if not config.settings.auth_enabled:
        return dict(ANONYMOUS_ADMIN)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    try:
        payload = app_jwt.decode(config.settings.jwt_secret, token)
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}")

    username = payload.get("sub")
    user_entry = config.users.get(username)
    if not user_entry:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User no longer authorized")

    return {
        "username": username,
        "role": user_entry.role,
        "avatar_url": payload.get("avatar_url"),
    }


def require_role(minimum: str) -> Callable[..., dict]:
    min_rank = ROLE_RANK[minimum]

    def dep(user: dict = Depends(current_user)) -> dict:
        if ROLE_RANK.get(user["role"], -1) < min_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{minimum}' or higher",
            )
        return user

    return dep
