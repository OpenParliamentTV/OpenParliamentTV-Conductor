"""Slack webhook notifier.

Follows the message format in `_planning/optv-import-manager-plan.md §Slack Notifications`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from src.config import SlackConfig

logger = logging.getLogger(__name__)


@dataclass
class JobResult:
    job_id: str
    job_name: str
    parliament: str
    success: bool
    partial: bool
    stage: str | None
    duration_seconds: int
    sessions_total: int
    sessions_completed: int
    failed_sessions: list[dict] = field(default_factory=list)


class SlackNotifier:
    def __init__(self, webhook_url: str, base_url: str, config: SlackConfig) -> None:
        self.webhook_url = webhook_url
        self.base_url = base_url.rstrip("/")
        self.config = config

    async def notify(self, result: JobResult, source: str = "manual") -> bool:
        if not self.webhook_url or not self.config.enabled:
            return False
        if self.config.scheduled_only and source != "scheduled":
            return False

        if result.success and not result.partial:
            if not self.config.on_success:
                return False
        elif result.partial:
            if not self.config.on_partial_failure:
                return False
        else:
            if not self.config.on_failure:
                return False

        message = self._build_message(result)
        return await self._send(message)

    def _build_message(self, result: JobResult) -> dict:
        job_url = f"{self.base_url}/jobs/{result.job_id}"
        duration = self._format_duration(result.duration_seconds)

        if result.success and not result.partial:
            emoji, title, color = "🟢", f"OPTV Import Complete: {result.job_name}", "good"
        elif result.partial:
            emoji, title, color = "🟡", f"OPTV Import Partial: {result.job_name}", "warning"
        else:
            emoji, title, color = "🔴", f"OPTV Import Failed: {result.job_name}", "danger"

        fields = [
            {"title": "Duration", "value": duration, "short": True},
            {"title": "Sessions", "value": f"{result.sessions_completed}/{result.sessions_total}", "short": True},
        ]
        if result.stage and not result.success:
            fields.insert(0, {"title": "Failed at stage", "value": result.stage, "short": True})

        text_parts: list[str] = []
        if result.failed_sessions:
            text_parts.append("*Failed sessions:*")
            for fs in result.failed_sessions[:10]:
                text_parts.append(f"• {fs.get('session')}: {fs.get('error')}")
            if len(result.failed_sessions) > 10:
                text_parts.append(f"_…and {len(result.failed_sessions) - 10} more_")

        return {
            "attachments": [
                {
                    "color": color,
                    "title": f"{emoji} {title}",
                    "title_link": job_url,
                    "fields": fields,
                    "text": "\n".join(text_parts) if text_parts else None,
                    "footer": "OpenParliamentTV-Conductor",
                    "ts": int(time.time()),
                }
            ]
        }

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m"

    async def _send(self, message: dict) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.webhook_url, json=message, timeout=10.0)
                if response.status_code == 200:
                    logger.info("Slack notification sent for job %s", message["attachments"][0]["title"])
                    return True
                logger.error("Slack notification failed: %s", response.status_code)
                return False
        except Exception as exc:
            logger.error("Slack notification error: %s", exc)
            return False
