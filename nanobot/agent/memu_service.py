"""memU bridge: proactive memory extraction and retrieval."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import MemUConfig


class MemUBridge:
    """Bridge between nanobot's agent loop and the memU memory service.

    Gracefully degrades to no-ops when memu-py is not installed.
    """

    def __init__(self, config: MemUConfig):
        self._config = config
        self._service: Any = None  # memu.MemUService instance (lazy)
        self._buffer: list[dict[str, str]] = []
        self._last_flush: float = time.monotonic()
        self._bg_task: asyncio.Task | None = None
        self._available = False

    async def initialize(self) -> None:
        """Lazy-import memu and create the service. No-op if unavailable."""
        if not self._config.enabled:
            return

        try:
            from memu import MemUService  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "memU enabled in config but memu-py is not installed. "
                "Install with: pip install 'nanobot-ai[memory]'"
            )
            return

        db_path = Path(self._config.db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        kwargs: dict[str, Any] = {"db_path": str(db_path)}
        if self._config.extraction_model:
            kwargs["model"] = self._config.extraction_model

        try:
            self._service = MemUService(**kwargs)
            self._available = True
            logger.info("memU service initialized (db={})", db_path)
        except Exception as e:
            logger.error("Failed to initialize memU service: {}", e)

    def start_background_task(self) -> None:
        """Start the periodic flush check as an asyncio background task."""
        if not self._available:
            return
        self._bg_task = asyncio.create_task(self._periodic_flush())

    async def _periodic_flush(self) -> None:
        """Check flush thresholds every 10 seconds."""
        try:
            while True:
                await asyncio.sleep(10)
                elapsed = time.monotonic() - self._last_flush
                if self._buffer and elapsed >= self._config.batch_time_threshold_s:
                    await self._flush()
        except asyncio.CancelledError:
            pass

    def feed_messages(self, messages: list[dict]) -> None:
        """Buffer user/assistant text messages from a turn for later extraction."""
        if not self._available:
            return

        for msg in messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            self._buffer.append({"role": role, "content": content})

        if len(self._buffer) >= self._config.batch_message_threshold:
            asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        """Send buffered messages to memU for extraction."""
        if not self._buffer or not self._service:
            return

        batch = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.monotonic()

        try:
            text = "\n\n".join(f"[{m['role']}]: {m['content']}" for m in batch)
            await asyncio.to_thread(self._service.memorize, text)
            logger.debug("memU memorized {} messages", len(batch))
        except Exception as e:
            logger.error("memU memorize failed: {}", e)

    async def retrieve(self, query: str) -> str:
        """Flush pending messages, then retrieve relevant memories."""
        if not self._available or not self._service:
            return "memU service is not available."

        # Flush pending buffer before querying so recent context is included
        await self._flush()

        try:
            results = await asyncio.to_thread(
                self._service.retrieve, query, top_k=self._config.max_retrieval_results
            )
        except Exception as e:
            logger.error("memU retrieve failed: {}", e)
            return f"Memory retrieval error: {e}"

        if not results:
            return "No relevant memories found."

        lines = [f"Found {len(results)} relevant memory/memories:\n"]
        for i, r in enumerate(results, 1):
            fact = r.get("fact") or r.get("text") or str(r)
            score = r.get("score")
            score_str = f" (relevance: {score:.2f})" if score is not None else ""
            lines.append(f"{i}. {fact}{score_str}")
        return "\n".join(lines)

    async def close(self) -> None:
        """Cancel background task, flush remaining, close service."""
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass

        if self._available:
            await self._flush()

        if self._service and hasattr(self._service, "close"):
            try:
                self._service.close()
            except Exception:
                pass

        logger.debug("memU bridge closed")
