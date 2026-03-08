from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.session.manager import Session


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_CHARS = 500
    loop._memu_bridge = None
    return loop


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": runtime},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_persists_usage_on_final_assistant_message() -> None:
    loop = _mk_loop()
    session = Session(key="test:usage")

    loop._save_turn(
        session,
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read"}}],
            },
            {
                "role": "assistant",
                "content": "Done.",
            },
        ],
        skip=0,
        usage={"prompt_tokens": 1000, "completion_tokens": 42},
        model="openai-codex/gpt-5.3-codex",
    )

    assert session.messages[-1].get("usage", {}).get("completion_tokens") == 42
    assert session.messages[-1].get("model") == "openai-codex/gpt-5.3-codex"
