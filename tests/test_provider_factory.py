from nanobot.config.schema import Config
from nanobot.providers.factory import make_provider
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import OpenAICodexProvider, _strip_model_prefix


def test_make_provider_uses_openrouter_for_explicit_openrouter_model() -> None:
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.4"
    config.providers.openrouter.api_key = "sk-or-test"

    provider = make_provider(
        config,
        model_override="openrouter/google/gemini-2.0-flash-lite-001",
        current_provider=OpenAICodexProvider(),
    )

    assert isinstance(provider, LiteLLMProvider)
    assert provider.default_model == "openrouter/google/gemini-2.0-flash-lite-001"


def test_make_provider_prefers_codex_for_openai_models_without_api_key() -> None:
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.4"

    provider = make_provider(
        config,
        model_override="openai/gpt-5-mini",
        current_provider=OpenAICodexProvider(),
    )

    assert isinstance(provider, OpenAICodexProvider)
    assert provider.default_model == "openai/gpt-5-mini"


def test_make_provider_keeps_openai_api_provider_when_key_exists() -> None:
    config = Config()
    config.providers.openai.api_key = "sk-test"

    provider = make_provider(config, model_override="openai/gpt-5-mini")

    assert isinstance(provider, LiteLLMProvider)
    assert provider.default_model == "openai/gpt-5-mini"


def test_codex_strip_model_prefix_accepts_openai_alias() -> None:
    assert _strip_model_prefix("openai/gpt-5-mini") == "gpt-5-mini"
