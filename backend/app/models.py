"""Model configuration — all models route through OpenCode.

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
# All route through OpenCode with a single OPENCODE_API_KEY.
# supports_images: when False, multimodal content is stripped before sending.

MODELS = {
    "deepseek_v4_flash": {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "provider": "OpenCode",
        "max_tokens": 65536,
        "supports_images": False,
    },
    "glm_52": {
        "id": "glm-5.2",
        "name": "GLM 5.2",
        "provider": "OpenCode",
        "max_tokens": 65536,
        "supports_images": True,
    },
    "minimax_m3": {
        "id": "minimax-m3",
        "name": "MiniMax M3",
        "provider": "OpenCode",
        "max_tokens": 65536,
        "supports_images": True,
    },
}

BASE_URL = "https://opencode.ai/zen/go/v1"
API_KEY_ENV = "OPENCODE_API_KEY"


def make_model(model_key: str) -> OpenAILike:
    """Construct a model instance from a MODELS key."""
    info = MODELS[model_key]
    cls = TextOnlyOpenAILike if not info.get("supports_images", False) else OpenAILike
    return cls(
        id=info["id"],
        api_key=os.environ.get(API_KEY_ENV),
        base_url=BASE_URL,
        max_tokens=info["max_tokens"],
    )


def primary_model() -> OpenAILike:
    """Default model — DeepSeek V4 Flash (fast, great for learning)."""
    return make_model("deepseek_v4_flash")
