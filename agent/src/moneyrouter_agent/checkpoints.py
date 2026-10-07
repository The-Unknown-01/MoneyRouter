"""Durable checkpoints and JSON context for dependencies held outside graph state."""
from __future__ import annotations

import json
import os
import sqlite3
import weakref
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from .config import AGENT_ROOT


def make_checkpointer(kind: str, allowed: list):
    serde = JsonPlusSerializer(allowed_msgpack_modules=allowed)
    directory = os.getenv("MONEYROUTER_CHECKPOINT_DIR", str(AGENT_ROOT / ".data/checkpoints"))
    if directory == ":memory:":
        return InMemorySaver(serde=serde)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / f"{kind}.sqlite", check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn, serde=serde)
    saver.setup()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_context (thread_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.commit()
    weakref.finalize(saver, conn.close)
    return saver


def context(checkpointer, thread_id: str, value: dict | None = None):
    if not isinstance(checkpointer, SqliteSaver):
        return None
    with checkpointer.lock:
        checkpointer.conn.execute("CREATE TABLE IF NOT EXISTS agent_context (thread_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        if value is not None:
            with checkpointer.conn:
                checkpointer.conn.execute("INSERT OR REPLACE INTO agent_context VALUES (?, ?)",
                                          (thread_id, json.dumps(value, ensure_ascii=False)))
            return value
        row = checkpointer.conn.execute("SELECT data FROM agent_context WHERE thread_id=?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row else None
