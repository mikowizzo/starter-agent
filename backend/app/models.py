"""Model configuration. Models route through OpenCode by default, with
per-model overrides for other providers (e.g. Synthetic API).

Students get a single OPENCODE_API_KEY and can switch between models
via the UI model selector. To add a new model, add an entry to MODELS.
"""

import os
from typing import Any

from agno.models.openai.like import OpenAILike


class TextOnlyOpenAILike(OpenAILike):
    """OpenAILike variant that strips image/audio/file content blocks.

    Use for models that only accept text — prevents API errors when
    the conversation history contains multimodal blocks.
    """

    def _format_message(
        self, message, compress_tool_results: bool = False
    ) -> dict[str, Any]:
        if message.images or message.audio or message.files or message.videos:
            message = message.model_copy(
                update={
                    "images": None,
                    "audio": None,
                    "files": None,
                    "videos": None,
                }
            )
        return super()._format_message(message, compress_tool_results)


# ── Available models ────────────────────────────────────────────────
# Default route: OpenCode with a single OPENCODE_API_KEY.
# Per-model overrides: set "base_url" and/or "api_key_env" to route a model
# elsewhere (e.g. Synthetic API). supports_images: when False, multimodal
# content is stripped before sending.

MODELS = {
    "deepseek_v4_flash": {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "provider": "OpenCode",
        "max_tokens": 65536,
        "supports_images": False,
    },
    "glm_53": {
        "id": "GLM-5.3",
        "name": "GLM 5.3",
        "provider": "ZAI",
        "base_url": "https://api.z.ai/api/coding/paas/v4",
        "api_key_env": "ZAI_API_KEY",
        "max_tokens": 65536,
        "supports_images": False,
        # Z.AI thinking control — GLM 5.3 supports {type, level}
        # where level ∈ {low, medium, high}. Default is medium.
        "thinking": {"type": "enabled", "level": "high"},
    },
    "minimax_m3": {
        "id": "minimax-m3",
        "name": "MiniMax M3",
        "provider": "OpenCode",
        "max_tokens": 65536,
        "supports_images": True,
    },
    "deepseek_v4_flash_or": {
        "id": "deepseek/deepseek-v4-flash-0731",
        "name": "DeepSeek V4 Flash (OpenRouter)",
        "provider": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "max_tokens": 65536,
        "supports_images": False,
    },
    "kimi_k3_synthetic": {
        "id": "hf:moonshotai/Kimi-K3",
        "name": "Kimi K3 (Synthetic)",
        "provider": "Synthetic",
        "max_tokens": 65536,
        "supports_images": False,
        "base_url": "https://api.synthetic.new/v1",
        "api_key_env": "SYNTHETIC_API_KEY",
    },
}

BASE_URL = "https://opencode.ai/zen/go/v1"
API_KEY_ENV = "OPENCODE_API_KEY"


def make_model(model_key: str) -> OpenAILike:
    """Construct a model instance from a MODELS key."""
    info = MODELS[model_key]
    cls = TextOnlyOpenAILike if not info.get("supports_images", False) else OpenAILike
    extra_body = info.get("thinking") or None
    return cls(
        id=info["id"],
        api_key=os.environ.get(info.get("api_key_env", API_KEY_ENV)),
        base_url=info.get("base_url", BASE_URL),
        max_tokens=info["max_tokens"],
        extra_body=extra_body,
    )


def primary_model() -> OpenAILike:
    """Default model — GLM 5.3."""
    return make_model("glm_53")
