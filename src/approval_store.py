"""
Pending approval 요청을 SQLite에 저장하고 조회/업데이트하는 스토어.

approval_server.py의 FastAPI 엔드포인트와 executor.py 데몬 모드가 공유한다.
"""
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

_DB_PATH       = "./data/agent_metrics.db"
_lock          = threading.Lock()
EXPIRY_MINUTES = 10  # 토큰 유효 시간 (분)


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
                expires_at TEXT NOT NULL,
                decided_at TEXT
            )
        """)
        # 기존 테이블에 expires_at 컬럼이 없으면 추가 (마이그레이션)
        try:
            conn.execute("ALTER TABLE pending_approvals ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        conn.commit()
        conn.close()


def create_request(command: str, error_log: str, reason: str) -> str:
    """새 승인 요청을 삽입하고 토큰을 반환한다."""
    token      = secrets.token_urlsafe(32)
    now        = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=EXPIRY_MINUTES)).isoformat()
    with _lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO pending_approvals (token, command, error_log, reason, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, command, error_log, reason, now.isoformat(), expires_at),
        )
        conn.commit()
        conn.close()
    return token


def get_status(token: str) -> str | None:
    """'pending' | 'approved' | 'rejected' | 'expired' | None(미존재)"""
    conn = _conn()
    row  = conn.execute(
        "SELECT status, expires_at FROM pending_approvals WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    if row["status"] == "pending" and row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > exp:
                return "expired"
        except ValueError:
            pass
    return row["status"]


def get_request(token: str) -> dict | None:
    """토큰에 해당하는 요청 전체 정보를 반환한다. 없으면 None."""
    conn = _conn()
    row  = conn.execute(
        "SELECT command, error_log, status, created_at, expires_at FROM pending_approvals WHERE token = ?",
        (token,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


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
