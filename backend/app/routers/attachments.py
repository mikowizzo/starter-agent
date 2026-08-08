"""Attachment upload + text extraction + status polling + prompt assembly.

Flow:
  1. Client POSTs files (+ session_id) -> raw bytes stored under a
     server-generated uuid name, returns {id, status:"pending"} immediately.
  2. A FastAPI BackgroundTask converts the file to text (markitdown) and
     flips status to ready/failed. Client polls GET /attachments/{id}.
  3. Before sending a chat run, client POSTs /attachments/assemble with the
     attachment ids -> gets back the budgeted <attachments> block to append
     to the message, and the exact snapshot is persisted to
     message_attachments (audit trail / deterministic replay).

The raw file always survives on disk even when conversion fails, and the
failed block tells the agent exactly where it is — no more silent stubs.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

import requests

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.db import db
from app.services.prompt_assembly import build_attachment_block, estimate_tokens

router = APIRouter(prefix="/attachments", tags=["attachments"])

UPLOAD_DIR = Path(os.getenv("ATTACHMENT_DIR", "/workspace/uploads/attachments"))
MAX_BYTES = 50 * 1024 * 1024          # 50 MB hard cap (matches /convert)
MAX_STORED_CHARS = 500_000            # cap extracted text we persist
CONVERT_TIMEOUT_S = 120               # markitdown can hang on malformed files

# ── Vision extraction for images ─────────────────────────────────────
# markitdown can't OCR photos, so images are described by a vision model
# (MiniMax M3 via OpenCode). Documents keep going through markitdown.
VISION_MODEL = "minimax-m3"
VISION_ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"
VISION_MAX_EDGE = 1280                # downscale long edge to keep payload sane
VISION_QUALITY = 85                   # JPEG re-encode quality
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
VISION_PROMPT = (
    "Describe this image in detail, factually. Include any visible text, signs, "
    "people, vehicles, buildings, property features, or landmarks. Be concise."
)

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class AssembleRequest(BaseModel):
    message_id: str
    attachment_ids: list[str] = []
    session_id: str | None = None


def _row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "session_id": r["session_id"],
        "filename": r["filename"],
        "mime_type": r["mime_type"],
        "size_bytes": r["size_bytes"],
        "status": r["status"],
        "token_count": r["token_count"],
        "error": r["error"],
        "created_at": r["created_at"],
        "storage_path": r["storage_path"],
    }


def _get_or_404(attachment_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"attachment {attachment_id} not found")
    return row


def _extract_text(path: Path) -> str:
    """Convert a file to text via markitdown. Raises on any failure."""
    from markitdown import MarkItDown

    result = MarkItDown().convert(str(path))
    return result.text_content or ""


def _is_image(mime_type: str | None, filename: str) -> bool:
    if mime_type and mime_type.startswith("image/"):
        return True
    return Path(filename or "").suffix.lower() in IMAGE_EXTS


def _describe_image(path: Path) -> str:
    """Ask the vision model (MiniMax M3 via OpenCode) what's in the photo."""
    import base64
    import io

    from PIL import Image

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
    return (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _convert(attachment_id: str) -> None:
    """Background task: extract text, estimate tokens, flip status."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE attachments SET status='processing', updated_at=datetime('now') "
            "WHERE id = ?",
            (attachment_id,),
        )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            if _is_image(row["mime_type"], row["filename"]):
                future = pool.submit(_describe_image, Path(row["storage_path"]))
            else:
                future = pool.submit(_extract_text, Path(row["storage_path"]))
            try:
                text = future.result(timeout=CONVERT_TIMEOUT_S)
            except FutureTimeout:
                raise TimeoutError(
                    f"conversion timed out after {CONVERT_TIMEOUT_S}s"
                )
        text = (text or "")[:MAX_STORED_CHARS]
        with db() as conn:
            conn.execute(
                "UPDATE attachments SET status='ready', extracted_text=?, "
                "token_count=?, error=NULL, updated_at=datetime('now') WHERE id=?",
                (text, estimate_tokens(text), attachment_id),
            )
    except Exception as e:  # any failure => 'failed' with a reason
        with db() as conn:
            conn.execute(
                "UPDATE attachments SET status='failed', error=?, "
                "updated_at=datetime('now') WHERE id=?",
                (str(e)[:500], attachment_id),
            )


@router.post("", status_code=202)
async def upload_attachments(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    session_id: str = Form(...),
):
    """Upload one or more files. Returns immediately; poll GET /attachments/{id}."""
    results = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{f.filename}: exceeds {MAX_BYTES} bytes")

        attachment_id = uuid.uuid4().hex
        suffix = Path(f.filename or "file").suffix.lower()
        dest = UPLOAD_DIR / f"{attachment_id}{suffix}"
        dest.write_bytes(data)

        with db() as conn:
            conn.execute(
                "INSERT INTO attachments (id, session_id, filename, mime_type, "
                "size_bytes, status, storage_path) VALUES (?,?,?,?,?, 'pending', ?)",
                (attachment_id, session_id, f.filename or "unnamed",
                 f.content_type, len(data), str(dest)),
            )
        background_tasks.add_task(_convert, attachment_id)
        results.append({
            "id": attachment_id,
            "session_id": session_id,
            "filename": f.filename,
            "status": "pending",
            "token_count": None,
            "error": None,
        })
    return {"attachments": results}


@router.get("/{attachment_id}")
async def get_attachment(attachment_id: str):
    return _row_to_dict(_get_or_404(attachment_id))


@router.get("")
async def list_attachments(session_id: str):
    """All attachments for a session (used to rebuild manifest on reload)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
    return {"attachments": [_row_to_dict(r) for r in rows]}


@router.post("/{attachment_id}/retry", status_code=202)
async def retry_attachment(attachment_id: str, background_tasks: BackgroundTasks):
    row = _get_or_404(attachment_id)
    if row["status"] not in ("failed", "ready"):
        raise HTTPException(409, f"cannot retry while status='{row['status']}'")
    with db() as conn:
        conn.execute(
            "UPDATE attachments SET status='pending', error=NULL, "
            "updated_at=datetime('now') WHERE id=?",
            (attachment_id,),
        )
    background_tasks.add_task(_convert, attachment_id)
    return {"id": attachment_id, "status": "pending"}


@router.post("/assemble")
async def assemble(req: AssembleRequest):
    """Server-side, budgeted attachment block for a chat message.

    Persists the exact snapshot to message_attachments and returns the block
    the frontend appends to the user message before POSTing to /teams/.../runs.
    """
    block = build_attachment_block(
        req.message_id,
        req.attachment_ids,
        session_id=req.session_id,
    )
    return {"text": block}
