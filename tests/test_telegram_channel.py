from types import SimpleNamespace

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel
from nanobot.config.schema import TelegramConfig


def _message(chat_type: str, chat_id: int, thread_id: int | None, *, is_forum: bool = False):
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, is_forum=is_forum),
        chat_id=chat_id,
        message_id=55,
        message_thread_id=thread_id,
    )


def _user():
    return SimpleNamespace(id=12345, username="zarred", first_name="Zarred")


def test_derive_topic_session_key_for_forum_topic() -> None:
    key = TelegramChannel._derive_topic_session_key(
        _message("supergroup", -1001, 42, is_forum=True)
    )

    assert key == "telegram:-1001:topic:42"


def test_derive_topic_session_key_skips_private_chat() -> None:
    key = TelegramChannel._derive_topic_session_key(_message("private", 12345, 42))

    assert key is None


def test_build_message_metadata_includes_thread_fields() -> None:
    metadata = TelegramChannel._build_message_metadata(
        _message("supergroup", -1001, 42, is_forum=True),
        _user(),
    )

    assert metadata["message_thread_id"] == 42
    assert metadata["is_forum"] is True
    assert metadata["is_group"] is True


def test_topic_session_key_takes_precedence_over_default_session() -> None:
    channel = TelegramChannel(
        TelegramConfig(allow_from=["12345|zarred"]),
        MessageBus(),
        default_session="user:zarred",
    )

    sender_id = channel._sender_id(_user())
    topic_key = TelegramChannel._derive_topic_session_key(
        _message("supergroup", -1001, 42, is_forum=True)
    )

    assert channel._matches_allow_entry(sender_id, channel.config.allow_from[0]) is True
    assert topic_key == "telegram:-1001:topic:42"
