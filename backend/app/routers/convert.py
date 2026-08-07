"""Document-to-markdown conversion endpoint.

Upload files, get markdown text back. Used by the frontend before
sending a message so document content reaches the LLM as readable text
(instead of raw binary bytes that OpenAI-compatible APIs can't parse).

Files are saved to /workspace/uploads/ before conversion so the agent
always has access to the raw file even if markitdown can't parse it.
"""

from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(tags=["convert"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
UPLOAD_DIR = Path("/workspace/uploads")


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

        # Save the file to a persistent location so the agent can access
        # the raw file even if markitdown can't convert it.
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path(f.filename or "upload").name
        saved_path = UPLOAD_DIR / safe_name

        # Avoid collisions: append a counter if the name already exists
        counter = 1
        while saved_path.exists():
            stem = saved_path.stem
            suffix = saved_path.suffix
            saved_path = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1

        saved_path.write_bytes(raw)

        # Attempt conversion from the saved file
        try:
            result = md.convert(str(saved_path))
            content = result.text_content or "(empty document)"
            parts.append(
                f"\n\n--- **{f.filename}** (saved to {saved_path}) ---\n{content}\n"
            )
        except Exception as e:
            parts.append(
                f"\n\n--- **{f.filename}** (saved to {saved_path}, "
                f"conversion failed: {e}) ---\n"
            )

    return JSONResponse({"text": "".join(parts)})
