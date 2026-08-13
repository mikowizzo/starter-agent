"""Shared file-conversion utilities.

Two conversion paths:
  1. Office/PDF documents → markdown text via markitdown
  2. Images → textual description via a vision LLM (MiniMax M3 via OpenCode)

Both ``code_tools.read`` (on-the-fly binary file reading) and
``attachments._convert`` (upload-time extraction) call into here so the
logic lives in one place.
"""

import base64
import io
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
CONVERT_TIMEOUT_S = 120

# markitdown can handle office docs, PDFs, and HTML
MARKITDOWN_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".odt", ".odp", ".ods", ".rtf", ".epub", ".html", ".htm",
}

# Images go through the vision model
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
}

# ── Vision settings ───────────────────────────────────────────────────
VISION_MODEL = "minimax-m3"
VISION_ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"
VISION_MAX_EDGE = 1280
VISION_QUALITY = 85
VISION_PROMPT = (
    "Describe this image in detail, factually. Include any visible text, signs, "
    "people, vehicles, buildings, property features, or landmarks. Be concise."
)

_markitdown_instance = None


# ── Public helpers ────────────────────────────────────────────────────
def is_document(path: Path) -> bool:
    return path.suffix.lower() in MARKITDOWN_EXTS


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def convert_document(path: Path) -> str | None:
    """Convert an office/PDF/HTML file to markdown via markitdown."""
    global _markitdown_instance
    if _markitdown_instance is None:
        from markitdown import MarkItDown
        _markitdown_instance = MarkItDown()
    try:
        result = _markitdown_instance.convert(str(path))
        text = result.text_content
        if text and text.strip():
            return text
    except Exception as exc:
        logger.warning("markitdown failed on %s: %s", path, exc)
    return None


def describe_image(path: Path) -> str | None:
    """Send an image to the vision model and return its description."""
    from PIL import Image

    try:
        img = Image.open(path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail((VISION_MAX_EDGE, VISION_MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=VISION_QUALITY)
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

        resp = requests.post(
            VISION_ENDPOINT,
            headers={
                "Authorization": f"Bearer {os.environ['OPENCODE_API_KEY']}",
                "Content-Type": "application/json",
                # Cloudflare blocks the python-requests default UA (error 1010).
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            },
            json={
                "model": VISION_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            },
            timeout=CONVERT_TIMEOUT_S,
        )
        resp.raise_for_status()
        text = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if text.strip():
            return text.strip()
    except Exception as exc:
        logger.warning("vision model failed on %s: %s", path, exc)
    return None


def convert_to_text(path: Path) -> tuple[str | None, str | None]:
    """Try all conversion paths.

    Returns ``(text, method)`` where *method* is ``"markitdown"`` or
    ``"vision"``, or ``(None, None)`` if nothing worked.
    """
    if is_image(path):
        text = describe_image(path)
        if text:
            return text, "vision"
    if is_document(path):
        text = convert_document(path)
        if text:
            return text, "markitdown"
    return None, None
