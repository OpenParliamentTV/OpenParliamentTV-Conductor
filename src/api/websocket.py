"""Websocket endpoints for live log + status streaming.

Note: auth is bearer-token-over-cookie; the browser sends the `optv_token`
cookie with the WS handshake so we reuse `current_user`.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.auth import jwt as app_jwt
from src.config import get_config
from src.services.registry import get_job_manager, get_log_streamer

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)


def _authorize(websocket: WebSocket) -> str | None:
    token = websocket.cookies.get("optv_token")
    if not token:
        return None
    config = get_config()
    try:
        payload = app_jwt.decode(config.settings.jwt_secret, token)
    except Exception:
        return None
    username = payload.get("sub")
    if not username or not config.users.get(username):
        return None
    return username


@router.websocket("/ws/logs/{job_id}")
async def ws_logs(websocket: WebSocket, job_id: str) -> None:
    user = _authorize(websocket)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    streamer = get_log_streamer()
    try:
        async for line in streamer.subscribe(job_id, include_backlog=True):
            await websocket.send_text(line)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("WS /ws/logs/%s failed", job_id)


@router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    user = _authorize(websocket)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    jm = get_job_manager()
    last_sent: str | None = None
    try:
        while True:
            current = jm.current()
            payload = json.dumps({"current": current.to_dict() if current else None}, sort_keys=True)
            if payload != last_sent:
                await websocket.send_text(payload)
                last_sent = payload
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
