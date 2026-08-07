"""Document-to-markdown conversion endpoint.

Upload files, get markdown text back. Used by the frontend before
sending a message so document content reaches the LLM as readable text
(instead of raw binary bytes that OpenAI-compatible APIs can't parse).
"""

import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(tags=["convert"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/convert")
async def convert_files(files: list[UploadFile] = File(...)):
    """Convert uploaded documents to markdown text."""
    try:
        from markitdown import MarkItDown
    except ImportError:
        raise HTTPException(503, "markitdown not installed on the server")

    md = MarkItDown()
    parts: list[str] = []

    for f in files:
        raw = await f.read()
        if len(raw) > MAX_FILE_SIZE:
            parts.append(f"\n\n--- **{f.filename}** (skipped: exceeds 50 MB) ---\n")
            continue

        # markitdown needs a file path, so write to a temp file
        suffix = Path(f.filename or "").suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name

        try:
            result = md.convert(tmp_path)
            content = result.text_content or "(empty document)"
            parts.append(f"\n\n--- **{f.filename}** ---\n{content}\n")
        except Exception as e:
            parts.append(f"\n\n--- **{f.filename}** (conversion error: {e}) ---\n")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    return JSONResponse({"text": "".join(parts)})
