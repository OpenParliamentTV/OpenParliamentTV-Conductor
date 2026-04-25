"""Per-job log file + in-process WebSocket broadcast.

Writes go to `status/logs/jobs/<job_id>.log`. Each active WebSocket
subscriber receives every appended line. `subscribe()` returns an async
iterator of lines, so subscribing returns backlog + future lines.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import AsyncIterator


class LogStreamer:
    def __init__(self, status_dir: Path) -> None:
        self.root = Path(status_dir) / "logs" / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

    def log_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.log"

    async def append(self, job_id: str, line: str) -> None:
        text = line if line.endswith("\n") else line + "\n"
        # File append is synchronous but small; acceptable for our throughput.
        with self.log_path(job_id).open("a", encoding="utf-8") as fh:
            fh.write(text)
        for q in list(self._subscribers[job_id]):
            try:
                q.put_nowait(text.rstrip("\n"))
            except asyncio.QueueFull:
                pass

    def read(self, job_id: str) -> str:
        path = self.log_path(job_id)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def subscribe(self, job_id: str, include_backlog: bool = True) -> AsyncIterator[str]:
        """Async generator yielding log lines for this job."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers[job_id].add(queue)
        try:
            if include_backlog:
                for line in self.read(job_id).splitlines():
                    yield line
            while True:
                yield await queue.get()
        finally:
            self._subscribers[job_id].discard(queue)


_streamer: LogStreamer | None = None


def get_streamer(status_dir: Path) -> LogStreamer:
    global _streamer
    if _streamer is None:
        _streamer = LogStreamer(status_dir)
    return _streamer
