"""Shared SQLite handle for app-owned tables (attachments, message_attachments).

We reuse the same SQLite file agno uses for sessions, so there is exactly one
DB to back up/inspect, and message_attachments can reference session ids that
agno owns. WAL mode avoids read/write blocking between the API threads and the
background conversion tasks.
"""

import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    filename       TEXT NOT NULL,
    mime_type      TEXT,
    size_bytes     INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','processing','ready','failed')),
    storage_path   TEXT NOT NULL,
    extracted_text TEXT,                -- NULL until conversion succeeds
    token_count    INTEGER,             -- estimated tokens of extracted_text
    error          TEXT,                -- populated when status='failed'
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);
CREATE INDEX IF NOT EXISTS idx_attachments_created ON attachments(created_at);

CREATE TABLE IF NOT EXISTS message_attachments (
    message_id       TEXT NOT NULL,
    attachment_id    TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    mode             TEXT NOT NULL
                     CHECK (mode IN ('inline','excerpt','reference','failed')),
    inlined_snapshot TEXT,              -- exactly what the model saw
    token_count      INTEGER NOT NULL DEFAULT 0,
    position         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, attachment_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_att_message ON message_attachments(message_id);
"""


@contextmanager
def db():
    """Yield a committed-on-success sqlite3 connection (WAL, Row factory)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_attachment_tables() -> None:
    """Create the app-owned tables if missing. Idempotent — safe at startup."""
    with db() as conn:
        conn.executescript(SCHEMA)
