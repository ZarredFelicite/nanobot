import pytest
from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ChannelsConfig, TelegramConfig


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


@pytest.mark.asyncio
async def test_message_tool_uses_owner_target_from_telegram_config(tmp_path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    channels = ChannelsConfig(telegram=TelegramConfig(allow_from=["8281248569|zarred"]))
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        memory_window=10,
        channels_config=channels,
    )

    sent: list[OutboundMessage] = []
    tool = loop.tools.get("message")
    assert isinstance(tool, MessageTool)
    tool.set_send_callback(lambda msg: _capture(sent, msg))

    tool.set_context("cli", "main")
    result = await tool.execute(content="hello", channel="email", chat_id="someone@example.com")

    assert "telegram:8281248569" in result
    assert len(sent) == 1
    assert sent[0].channel == "telegram"
    assert sent[0].chat_id == "8281248569"


async def _capture(sent: list[OutboundMessage], msg: OutboundMessage) -> None:
    sent.append(msg)
