"""
Pending approval 요청을 SQLite에 저장하고 조회/업데이트하는 스토어.

approval_server.py의 FastAPI 엔드포인트와 executor.py 데몬 모드가 공유한다.
"""
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

_DB_PATH = "./data/agent_metrics.db"
_lock    = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    with _lock:
        conn = _conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                token      TEXT PRIMARY KEY,
                command    TEXT NOT NULL,
                error_log  TEXT,
                reason     TEXT,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
        """)
        conn.commit()
        conn.close()


def create_request(command: str, error_log: str, reason: str) -> str:
    """새 승인 요청을 삽입하고 토큰을 반환한다."""
    token = secrets.token_urlsafe(32)
    now   = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO pending_approvals (token, command, error_log, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, command, error_log, reason, now),
        )
        conn.commit()
        conn.close()
    return token


def get_status(token: str) -> str | None:
    """'pending' | 'approved' | 'rejected' | None(미존재)"""
    conn = _conn()
    row  = conn.execute(
        "SELECT status FROM pending_approvals WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    return row["status"] if row else None


def set_decision(token: str, decision: str) -> bool:
    """decision: 'approved' 또는 'rejected'. 상태가 pending일 때만 업데이트. 성공 여부 반환."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _conn()
        cur  = conn.execute(
            "UPDATE pending_approvals SET status = ?, decided_at = ? "
            "WHERE token = ? AND status = 'pending'",
            (decision, now, token),
        )
        conn.commit()
        conn.close()
    return cur.rowcount == 1
