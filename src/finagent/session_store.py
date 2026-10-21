"""대화 이력·직전 슬롯을 SQLite에 저장"""

import json
import sqlite3
from pathlib import Path


class SqliteSessionStore:
    """thread_id별 대화 이력과 직전 슬롯을 SQLite에 저장한다."""

    def __init__(self, db_path: Path | str):
        """DB에 연결하고 messages 테이블이 없으면 만든다."""
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (thread_id TEXT, role "
            "TEXT, content TEXT)"
        )
        self.conn.commit()

    def load(self, thread_id: str) -> tuple[list[dict], dict | None]:
        """(대화 이력, 직전 슬롯 | None)을 불러온다."""
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE thread_id=? ORDER "
            "BY rowid",
            (thread_id,),
        ).fetchall()
        history = [{"role": r, "content": c} for r, c in rows if r != "slots"]
        slots = next(
            (json.loads(c) for r, c in reversed(rows) if r == "slots"), None
        )
        return history, slots

    def append(
        self, thread_id: str, user: str, assistant: str, slots: dict | None
    ) -> None:
        """사용자·답변 한 턴을 저장한다. 상품 대상이 있는 슬롯이면 함께 저장한다."""
        rows = [(thread_id, "user", user), (thread_id, "assistant", assistant)]
        if slots and (slots.get("kinds") or slots.get("product_ids")):
            rows.append(
                (thread_id, "slots", json.dumps(slots, ensure_ascii=False))
            )
        self.conn.executemany("INSERT INTO messages VALUES (?,?,?)", rows)
        self.conn.commit()

    def clear(self, thread_id: str) -> None:
        """지정 대화의 사용자·답변·슬롯을 함께 지운다."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM messages WHERE thread_id=?", (thread_id,)
            )

    def close(self) -> None:
        """DB 연결을 닫는다."""
        self.conn.close()
