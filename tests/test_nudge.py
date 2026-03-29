"""Tests for the background memory nudge feature."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_config(**overrides):
    """Create a mock SubconsciousConfig."""
    defaults = {
        "enabled": True,
        "extraction_model": "test-model",
        "classifier_model": "test-classifier",
        "auto_inject_budget": 1000,
        "auto_inject_results": 5,
        "batch_message_threshold": 5,
        "batch_time_threshold_s": 120,
        "compaction_enabled": False,
        "qmd_collection_name": "test-memory",
        "nudge_interval": 3,
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


def test_nudge_counter_increments():
    """Counter increments and fires at threshold."""
    from nanobot.agent.subconscious import SubconsciousService

    with patch.object(SubconsciousService, "__init__", lambda self, *a, **kw: None):
        svc = SubconsciousService.__new__(SubconsciousService)
        svc._nudge_counter = 0
        svc._config = _make_config(nudge_interval=3)

        assert not svc.increment_nudge_counter()  # 1
        assert not svc.increment_nudge_counter()  # 2
        assert svc.increment_nudge_counter()       # 3 — fires


def test_nudge_counter_reset():
    """Counter resets to zero."""
    from nanobot.agent.subconscious import SubconsciousService

    with patch.object(SubconsciousService, "__init__", lambda self, *a, **kw: None):
        svc = SubconsciousService.__new__(SubconsciousService)
        svc._nudge_counter = 5
        svc._config = _make_config(nudge_interval=3)

        svc.reset_nudge_counter()
        assert svc._nudge_counter == 0
        assert not svc.increment_nudge_counter()  # 1 again


def test_flush_resets_nudge_counter():
    """Organic extraction (_flush) resets the nudge counter."""
    from nanobot.agent.subconscious import SubconsciousService

    with patch.object(SubconsciousService, "__init__", lambda self, *a, **kw: None):
        svc = SubconsciousService.__new__(SubconsciousService)
        svc._nudge_counter = 5
        svc._config = _make_config(nudge_interval=10)
        svc._buffer = [{"role": "user", "content": "test"}]
        svc._provider = MagicMock()
        svc._last_flush = 0

        # Mock _extract to be a coroutine
        svc._extract = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(svc._flush())
        finally:
            loop.close()

        assert svc._nudge_counter == 0
