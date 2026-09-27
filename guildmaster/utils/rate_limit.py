"""Throttled async worker queue for Discord channel mutations.

Discord heavily rate-limits channel create/delete (~50 ops per 10 min).
Every mutation goes through this queue: one at a time, with a minimum
inter-operation delay plus exponential backoff on 429/5xx responses.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import discord

log = logging.getLogger(__name__)


@dataclass
class QueuedOp:
    label: str
    factory: Callable[[], Awaitable[Any]]
    result: Any = None
    error: Optional[Exception] = None
    attempts: int = field(default=0)


class ChannelOpQueue:
    def __init__(
        self,
        min_delay: float = 1.25,
        max_retries: int = 3,
        on_progress: Optional[Callable[[QueuedOp, int, int], Awaitable[None]]] = None,
    ) -> None:
        self.min_delay = min_delay
        self.max_retries = max_retries
        self.on_progress = on_progress
        self.queue: asyncio.Queue[QueuedOp] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self.completed = 0
        self.total = 0

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def submit(self, op: QueuedOp) -> None:
        self.total += 1
        await self.queue.put(op)

    async def join(self) -> None:
        await self.queue.join()

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            op = await self.queue.get()
            try:
                await self._execute(op)
            finally:
                self.completed += 1
                if self.on_progress is not None:
                    try:
                        await self.on_progress(op, self.completed, self.total)
                    except Exception:
                        log.exception("progress callback failed")
                self.queue.task_done()
                await asyncio.sleep(self.min_delay)

    async def _execute(self, op: QueuedOp) -> None:
        for attempt in range(1, self.max_retries + 1):
            op.attempts = attempt
            try:
                op.result = await op.factory()
                return
            except discord.HTTPException as exc:
                retry_after = getattr(exc, "retry_after", None)
                if exc.status == 429 or (retry_after is not None):
                    wait = float(retry_after or 2**attempt)
                    log.warning("Rate limited on %r; sleeping %.1fs", op.label, wait)
                    await asyncio.sleep(wait)
                    continue
                if exc.status >= 500 and attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                op.error = exc
                log.warning("Op %r failed: %s", op.label, exc)
                return
            except (discord.Forbidden, discord.NotFound) as exc:
                op.error = exc
                log.warning("Op %r denied/lost: %s", op.label, exc)
                return
            except Exception as exc:
                op.error = exc
                log.exception("Op %r raised unexpectedly", op.label)
                return
