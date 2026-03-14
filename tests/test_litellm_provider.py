from types import SimpleNamespace

from nanobot.providers.litellm_provider import LiteLLMProvider


def _tool_call(name: str, arguments: str):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def test_parse_response_merges_tool_calls_across_choices() -> None:
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(content="Let me do that", tool_calls=None),
            ),
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[_tool_call("read_file", '{"path": "README.md"}')],
                ),
            ),
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    parsed = provider._parse_response(response)

    assert parsed.content == "Let me do that"
    assert parsed.finish_reason == "tool_calls"
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "read_file"
    assert parsed.tool_calls[0].arguments == {"path": "README.md"}


def test_parse_response_handles_single_choice_without_tool_calls() -> None:
    provider = LiteLLMProvider(default_model="openrouter/minimax/minimax-m2.5")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="Done", tool_calls=None)
            )
        ],
        usage=None,
    )

    parsed = provider._parse_response(response)

    assert parsed.content == "Done"
    assert parsed.finish_reason == "stop"
    assert parsed.tool_calls == []


def test_sanitize_messages_truncates_long_tool_call_id() -> None:
    long_id = "x" * 80

    sanitized = LiteLLMProvider._sanitize_messages(
        [{"role": "tool", "tool_call_id": long_id, "content": "ok", "name": "exec"}]
    )

    assert len(sanitized[0]["tool_call_id"]) == 64
    assert sanitized[0]["tool_call_id"] == long_id[:32] + long_id[-32:]
