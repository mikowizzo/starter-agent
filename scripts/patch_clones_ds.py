#!/usr/bin/env python3
"""Add the OpenRouter DeepSeek model entry to every clone's models.py.

Idempotent: skips clones that already have "deepseek_v4_flash_or".
Verifies syntax with ast.parse after patching.
"""
import ast
import sys
from pathlib import Path

CLONES = Path("/workspace/.clones")
NAMES = ["franky", "luffy", "nami", "robin", "sanji", "usopp", "zoro"]

BLOCK = (
    '    "deepseek_v4_flash_or": {\n'
    '        "id": "deepseek/deepseek-v4-flash-0731",\n'
    '        "name": "DeepSeek V4 Flash (OpenRouter)",\n'
    '        "provider": "OpenRouter",\n'
    '        "base_url": "https://openrouter.ai/api/v1",\n'
    '        "api_key_env": "OPENROUTER_API_KEY",\n'
    '        "max_tokens": 65536,\n'
    '        "supports_images": False,\n'
    "    },\n"
)

# Insert before the kimi_k3_synthetic entry, after minimax_m3's closing brace.
ANCHOR = (
    '        "supports_images": True,\n'
    "    },\n"
    '    "kimi_k3_synthetic": {\n'
)

def patch(models_path: Path) -> str:
    if not models_path.is_file():
        return f"MISSING {models_path}"
    text = models_path.read_text()
    if "deepseek_v4_flash_or" in text:
        return "SKIP (already has it)"
    if ANCHOR not in text:
        # fallback: try the kimi anchor with different indentation
        return "FAIL (anchor not found — file structure differs)"
    text = text.replace(ANCHOR, BLOCK + ANCHOR, 1)
    ast.parse(text)  # raises on syntax errors
    models_path.write_text(text)
    return "OK"

ok, fail = [], []
for name in NAMES:
    p = CLONES / name / "backend/app/models.py"
    result = patch(p)
    (ok if result == "OK" else fail).append(f"{name}: {result}")
    print(f"{name}: {result}")

# Final verification pass
print("\nVerification:")
for name in NAMES:
    p = CLONES / name / "backend/app/models.py"
    if p.is_file():
        has = "deepseek_v4_flash_or" in p.read_text()
        try:
            ast.parse(p.read_text())
            syn = "syntax OK"
        except SyntaxError:
            syn = "SYNTAX ERROR"
        print(f"  {name}: entry={'yes' if has else 'no'}, {syn}")

print(f"\nDone: {len(ok)} patched, {len(fail)} failed")
sys.exit(1 if fail else 0)