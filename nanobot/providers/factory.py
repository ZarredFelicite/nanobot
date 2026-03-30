"""Provider factory helpers."""

from __future__ import annotations

from nanobot.config.schema import Config
from nanobot.providers.base import LLMProvider


def _should_use_codex_for_openai(
    config: Config, model: str, current_provider: LLMProvider | None
) -> bool:
    """Route OpenAI-family models through Codex OAuth when appropriate."""
    model_lower = model.lower()
    is_openai_family = model_lower.startswith("openai/") or model_lower.startswith("gpt-")
    if not is_openai_family:
        return False

    if model_lower.startswith(("openai-codex/", "openai_codex/")):
        return True

    provider_name = config.get_provider_name(model)
    if provider_name == "openai_codex":
        return True

    if current_provider is not None:
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        if isinstance(current_provider, OpenAICodexProvider):
            return True

    openai_cfg = config.providers.openai
    if not openai_cfg.api_key:
        return True

    return False


def make_provider(
    config: Config,
    model_override: str | None = None,
    *,
    current_provider: LLMProvider | None = None,
) -> LLMProvider:
    """Create the appropriate provider for a model using runtime config."""
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider
    from nanobot.providers.registry import find_by_name

    model = model_override or config.agents.defaults.model
    provider_name = config.get_provider_name(model)

    if model.startswith(("openai-codex/", "openai_codex/")) or _should_use_codex_for_openai(
        config, model, current_provider
    ):
        return OpenAICodexProvider(default_model=model)

    if provider_name is None:
        raise RuntimeError(f"Could not determine provider for model '{model}'.")

    provider_config = config.get_provider(model)

    if provider_name == "custom":
        return CustomProvider(
            api_key=provider_config.api_key if provider_config else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    spec = find_by_name(provider_name)
    if (
        not model.startswith("bedrock/")
        and not (provider_config and provider_config.api_key)
        and not (spec and spec.is_oauth)
    ):
        raise RuntimeError(f"No credentials configured for model '{model}'.")

    return LiteLLMProvider(
        api_key=provider_config.api_key if provider_config else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=provider_config.extra_headers if provider_config else None,
        provider_name=provider_name,
    )
