import sqlite3
import json
from datetime import datetime
from pathlib import Path

Path("db").mkdir(exist_ok=True)

def get_conn():
    return sqlite3.connect("db/chat.db", check_same_thread=False)

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            metadata TEXT,
            created_at TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
                       
        CREATE INDEX IF NOT EXISTS idx_messages_session 
        ON messages(session_id);
    """)
    conn.commit()
    conn.close()

def create_session(session_id: str, title: str):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?)",
        (session_id, title, now, now)
    )
    conn.commit()
    conn.close()

def update_session_time(session_id: str):
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), session_id)
    )
    conn.commit()
    conn.close()

def migrate_db():
    conn = get_conn()
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN tokens INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.close()

def save_message(session_id: str, role: str, content: str, metadata: dict = None, tokens: int = 0) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, metadata, tokens, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, role, content, json.dumps(metadata or {}), tokens, datetime.now().isoformat())
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id

def get_session_tokens(session_id: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT SUM(tokens) FROM messages WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    conn.close()
    return row[0] or 0

def get_messages(session_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content, metadata FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,)
    ).fetchall()
    conn.close()
    return [{"id": r[0], "role": r[1], "content": r[2], "metadata": json.loads(r[3])} for r in rows]

def truncate_from_message(session_id: str, from_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE session_id = ? AND id >= ?", (session_id, from_id))
    conn.commit()
    conn.close()

def get_sessions() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, updated_at FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]

def delete_session(session_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

def rename_session(session_id: str, title: str):
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET title = ? WHERE id = ?",
        (title, session_id)
    )
    conn.commit()
    conn.close()