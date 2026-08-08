"""Attachment retrieval tools for the agent.

Gives the model an explicit way to read uploaded files that were too large to
inline (or failed conversion) instead of relying on it remembering to poke
around /workspace/uploads/.

- read_attachment(id, offset, limit)  — page through the raw file on disk
- list_attachments(session_id)        — see everything uploaded in a session
"""

import sqlite3
from pathlib import Path

from agno.tools import Toolkit

from app.db import db

_READ_MAX_LINES = 2000


def _get_row(attachment_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    return dict(row) if row else None


class AttachmentTools(Toolkit):
    """Read uploaded attachments by id; list session attachments."""

    def __init__(self) -> None:
        super().__init__(
            name="attachment_tools",
            tools=[self.read_attachment, self.list_attachments],
        )

    def read_attachment(
        self, attachment_id: str, offset: int = 0, limit: int = 500
    ) -> str:
        """Read an uploaded file by its attachment id, paged by line.

        Args:
            attachment_id: The id from the <attachments> block or manifest
                (e.g. a 32-char hex string).
            offset: Zero-based line to start reading from.
            limit: Max lines to return (hard-capped at 2000).
        Returns numbered lines with a [read more with offset=N] hint.
        """
        row = _get_row(attachment_id)
        if not row:
            return f"❌ Attachment not found: {attachment_id}"
        if offset < 0:
            return "❌ offset must be >= 0"
        if limit <= 0:
            return "❌ limit must be > 0"

        path = Path(row["storage_path"])
        if not path.is_file():
            return (
                f"❌ Raw file missing for {row['filename']} ({attachment_id}): "
                f"{path}. Status was {row['status']}."
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"❌ Cannot read {path}: {e}"

        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return f"⚠️ Empty file: {row['filename']} ({attachment_id})"
        if offset >= total:
            return (
                f"❌ offset {offset} is past end of file ({total} lines). "
                f"Use offset=0."
            )
        slice_lines = lines[offset : offset + min(limit, _READ_MAX_LINES)]
        end = offset + len(slice_lines)
        width = len(str(end))
        numbered = [
            f"{offset + i + 1:>{width}}: {line}" for i, line in enumerate(slice_lines)
        ]
        out = (
            f"[lines {offset + 1}–{end} of {total} in {row['filename']} "
            f"(id={attachment_id}, status={row['status']})]\n"
            + "\n".join(numbered)
        )
        if end < total:
            out += f"\n[read more with offset={end}]"
        return out

    def list_attachments(self, session_id: str) -> str:
        """List all files uploaded in a session (id, name, status, tokens)."""
        with db() as conn:
            try:
                rows = conn.execute(
                    "SELECT id, filename, status, token_count, error, size_bytes "
                    "FROM attachments WHERE session_id = ? "
                    "ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            except sqlite3.Error as e:
                return f"❌ DB error: {e}"
        if not rows:
            return f"⚠️ No attachments found for session {session_id}"
        lines = [
            f"- `{r['filename']}` id={r['id']} status={r['status']} "
            f"size={r['size_bytes']}B"
            + (f" ~{r['token_count']} tokens" if r["token_count"] else "")
            + (f" error={r['error']}" if r["error"] else "")
            for r in rows
        ]
        return "\n".join(lines)
