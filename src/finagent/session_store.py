"""대화 이력·직전 슬롯을 SQLite에 저장 (베이스 agent/session_store.py와 같은 방식, 재시작해도 유지)."""
import json
import sqlite3
from pathlib import Path


class SqliteSessionStore:
    def __init__(self, db_path: Path | str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS messages (thread_id TEXT, role TEXT, content TEXT)")
        self.conn.commit()

    def load(self, thread_id: str) -> tuple[list[dict], dict | None]:
        rows = self.conn.execute("SELECT role, content FROM messages WHERE thread_id=? ORDER BY rowid",
                                 (thread_id,)).fetchall()
        history = [{"role": r, "content": c} for r, c in rows if r != "slots"]
        slots = next((json.loads(c) for r, c in reversed(rows) if r == "slots"), None)
        return history, slots

    def append(self, thread_id: str, user: str, assistant: str, slots: dict | None) -> None:
        rows = [(thread_id, "user", user), (thread_id, "assistant", assistant)]
        if slots and (slots.get("kinds") or slots.get("product_ids")):
            rows.append((thread_id, "slots", json.dumps(slots, ensure_ascii=False)))
        self.conn.executemany("INSERT INTO messages VALUES (?,?,?)", rows)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
