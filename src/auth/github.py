"""GitHub OAuth routes.

Flow:
  1. GET /auth/login            → Authlib redirects to GitHub authorize URL.
  2. GET /auth/callback?code=…  → exchange code for access token, fetch user,
                                  check users.yaml allow-list, issue JWT as
                                  httpOnly cookie, redirect to `/`.
  3. POST /auth/logout          → clear cookie.
  4. GET /auth/me               → current user info (requires auth).
"""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.auth import jwt as app_jwt
from src.auth.dependencies import current_user
from src.config import AppConfig, get_config

router = APIRouter(prefix="/auth", tags=["auth"])

_oauth: OAuth | None = None
COOKIE_NAME = "optv_token"


def _oauth_client(config: AppConfig) -> OAuth:
    global _oauth
    if _oauth is not None:
        return _oauth
    oauth = OAuth()
    oauth.register(
        name="github",
        client_id=config.settings.github_client_id,
        client_secret=config.settings.github_client_secret,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user"},
    )
    _oauth = oauth
    return oauth


@router.get("/login")
async def login(request: Request, config: AppConfig = Depends(get_config)):
    redirect_uri = f"{config.settings.base_url.rstrip('/')}/auth/callback"
    return await _oauth_client(config).github.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request, config: AppConfig = Depends(get_config)):
    try:
        token = await _oauth_client(config).github.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=f"OAuth error: {exc.error}")

    resp = await _oauth_client(config).github.get("user", token=token)
    user = resp.json()
    username = user.get("login")
    if not username:
        raise HTTPException(status_code=400, detail="GitHub did not return a username")

    allowed = config.users.get(username)
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"User '{username}' is not in users.yaml allow-list",
        )

    jwt_token = app_jwt.encode(
        config.settings.jwt_secret,
        {"sub": username, "role": allowed.role, "avatar_url": user.get("avatar_url")},
    )

    redirect = RedirectResponse(url="/", status_code=302)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        secure=config.settings.base_url.startswith("https://"),
        samesite="lax",
        max_age=12 * 3600,
    )
    return redirect


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user
