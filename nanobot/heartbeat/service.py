"""Heartbeat service - periodic agent wake-up for monitoring and social check-ins."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger


class HeartbeatService:
    """
    Periodic heartbeat service that delegates all work to an ``on_tick`` callback.

    The callback receives the HEARTBEAT.md content and orchestrates:
    1. A monitoring subagent (cheap model, throwaway session) for mechanical checks
    2. A main agent (full model, rich context) that decides what to surface

    The service itself is a simple timer — all nanobot-specific logic lives in
    the callback provided by the caller.
    """

    def __init__(
        self,
        workspace: Path,
        on_tick: Callable[[str], Coroutine[Any, Any, str]],
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.on_tick = on_tick
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        content = self._read_heartbeat_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: tick starting...")

        try:
            response = await self.on_tick(content)
            if response and self.on_notify:
                await self.on_notify(response)
        except Exception:
            logger.exception("Heartbeat tick failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        if not content:
            return None
        return await self.on_tick(content)
