from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.telegram import TelegramChannel
from nanobot.config.schema import TelegramConfig


class _DummyConfig:
    def __init__(self, allow_from):
        self.allow_from = allow_from


class _DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg) -> None:
        return None


def test_base_channel_requires_exact_allowlist_match() -> None:
    channel = _DummyChannel(_DummyConfig(["alice"]), MessageBus())

    assert channel.is_allowed("alice") is True
    assert channel.is_allowed("mallory|alice") is False


def test_telegram_channel_preserves_legacy_id_or_username_matching() -> None:
    bus = MessageBus()

    by_id = TelegramChannel(TelegramConfig(allow_from=["12345"]), bus)
    assert by_id.is_allowed("12345|zarred") is True

    by_username = TelegramChannel(TelegramConfig(allow_from=["zarred"]), bus)
    assert by_username.is_allowed("12345|zarred") is True


def test_telegram_channel_allows_exact_legacy_composite_entry() -> None:
    channel = TelegramChannel(TelegramConfig(allow_from=["12345|zarred"]), MessageBus())

    assert channel.is_allowed("12345|zarred") is True
    assert channel.is_allowed("12345|other") is False
