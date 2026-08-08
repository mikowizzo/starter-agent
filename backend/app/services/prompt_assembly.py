"""Token-budgeted attachment → prompt assembly.

Policy per attachment (evaluated smallest-first, greedy on budget):
  ready,  tokens <= INLINE_MAX and fits budget  -> INLINE full text
  ready,  larger but EXCERPT_TOKENS fits        -> EXCERPT head + elision note
  ready,  doesn't fit budget                    -> REFERENCE (manifest line only)
  failed / not ready                            -> FAILED (raw path + instruction)

Every decision is persisted to message_attachments with inlined_snapshot =
*exactly* what the model saw, so history is replayable/auditable and resume
flows never re-inline or lose content.
"""

from app.db import db

ATTACHMENT_TOKEN_BUDGET = 12_000   # max tokens of attachment content per message
INLINE_MAX = 2_000                 # inline anything at/below this
EXCERPT_TOKENS = 800               # head-excerpt size for medium files
MANIFEST_MAX = 10                  # cap sibling-file manifest lines
_CHARS_PER_TOKEN = 4               # heuristic, single seam for tiktoken later


def estimate_tokens(text: str) -> int:
    """~4 chars/token heuristic. Good enough for budgeting; no tokenizer dep."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _fmt(attachment_id: str, filename: str, mode: str, body: str) -> str:
    return (
        f'<attachment id="{attachment_id}" filename="{filename}" mode="{mode}">\n'
        f"{body}\n"
        f"</attachment>"
    )


def _note_block(attachment_id: str, storage_path: str) -> str:
    return (
        f"[Not inlined. Full content at: {storage_path} (id={attachment_id}). "
        f"Use the read_attachment tool with that id to read it.]"
    )


def build_attachment_block(
    message_id: str,
    attachment_ids: list[str],
    session_id: str | None = None,
    budget: int = ATTACHMENT_TOKEN_BUDGET,
) -> str:
    """Build the <attachments> block for a user message and persist snapshots.

    Returns "" when there are no attachments. Sibling files from the same
    session (not attached to this message) are listed in a compact manifest so
    the agent knows they exist and can read them via the read_attachment tool.
    """
    if not attachment_ids:
        return ""

    placeholders = ",".join("?" * len(attachment_ids))
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM attachments WHERE id IN ({placeholders})",
            attachment_ids,
        ).fetchall()
        order = {aid: i for i, aid in enumerate(attachment_ids)}
        # Smallest first (greedy budget), original order as tiebreak.
        rows.sort(key=lambda r: ((r["token_count"] or 1 << 30), order.get(r["id"], 0)))

        remaining = budget
        parts: list[tuple[int, str]] = []
        inserts: list[tuple] = []

        for r in rows:
            pos = order.get(r["id"], 0)
            aid, fname = r["id"], r["filename"]
            text = r["extracted_text"] or ""
            tok = r["token_count"] or estimate_tokens(text)

            if r["status"] != "ready":
                reason = (
                    f"processing failed: {r['error']}"
                    if r["status"] == "failed"
                    else f"processing {r['status']}"
                )
                body = (
                    f"[Attachment {reason}. Raw file at: {r['storage_path']} "
                    f"(id={aid}). If the user needs its contents, inspect the "
                    f"file directly or ask them to retry the upload.]"
                )
                parts.append((pos, _fmt(aid, fname, "failed", body)))
                inserts.append((message_id, aid, "failed", body, estimate_tokens(body), pos))
                continue

            if tok <= INLINE_MAX and tok <= remaining:
                mode, body, used = "inline", text, tok
            elif EXCERPT_TOKENS <= remaining:
                excerpt_chars = EXCERPT_TOKENS * _CHARS_PER_TOKEN
                note = (
                    f"\n\n[... excerpted: showing first ~{EXCERPT_TOKENS} of {tok} "
                    f"tokens. {_note_block(aid, r['storage_path'])}]"
                )
                mode, body = "excerpt", text[:excerpt_chars] + note
                used = estimate_tokens(body)
            else:
                body = (
                    f"[Not inlined: {tok} tokens exceeds remaining budget. "
                    f"{_note_block(aid, r['storage_path'])}]"
                )
                mode = "reference"
                used = estimate_tokens(body)

            remaining -= used
            parts.append((pos, _fmt(aid, fname, mode, body)))
            inserts.append((message_id, aid, mode, body, used, pos))

        conn.executemany(
            "INSERT INTO message_attachments "
            "(message_id, attachment_id, mode, inlined_snapshot, token_count, position) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(message_id, attachment_id) DO UPDATE SET "
            "mode=excluded.mode, inlined_snapshot=excluded.inlined_snapshot, "
            "token_count=excluded.token_count, position=excluded.position",
            inserts,
        )

    parts.sort(key=lambda p: p[0])
    block = "<attachments>\n" + "\n".join(p[1] for p in parts) + "\n</attachments>"

    if session_id:
        manifest = build_manifest(session_id, exclude_ids=set(attachment_ids))
        if manifest:
            block += "\n\n" + manifest

    return block


def build_manifest(session_id: str, exclude_ids: set[str] | None = None) -> str:
    """Compact list of other session attachments for the system/user prompt.

    Lets the agent know sibling files exist so it can read them on demand via
    the read_attachment tool, instead of silently ignoring them.
    """
    exclude_ids = exclude_ids or set()
    with db() as conn:
        rows = conn.execute(
            "SELECT id, filename, status, token_count, storage_path "
            "FROM attachments "
            "WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, MANIFEST_MAX + len(exclude_ids)),
        ).fetchall()
    rows = [r for r in rows if r["id"] not in exclude_ids][:MANIFEST_MAX]
    if not rows:
        return ""
    lines = [
        f"- `{r['filename']}` (id={r['id']}, {r['status']}"
        + (f", ~{r['token_count']} tokens" if r["token_count"] else "")
        + (f", path={r['storage_path']}" if r["status"] != "ready" else "")
        + ")"
        for r in rows
    ]
    return (
        "## Other files in this session\n"
        "The user has uploaded these files earlier in this session. They are "
        "NOT inlined above — if relevant, read them with the read_attachment "
        "tool by id.\n"
        + "\n".join(lines)
    )
