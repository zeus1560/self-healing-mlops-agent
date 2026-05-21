"""
Pending approval 요청을 SQLite에 저장·조회·업데이트하는 스토어.

approval_server.py의 FastAPI 엔드포인트와 executor.py 데몬 모드가 공유한다.

thread-safety:
  쓰기(create_request·set_decision·init_table)는 _lock으로 직렬화한다.
  읽기(get_status·get_request)는 SQLite WAL 모드에서 원자적 일관성이
  보장되므로 별도 lock이 불필요하지만, 모든 연결은 try/finally로
  예외 발생 여부와 무관하게 반드시 닫힌다.
"""
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

_DB_PATH       = "./data/agent_metrics.db"
_lock          = threading.Lock()
EXPIRY_MINUTES = 10  # 토큰 유효 시간 (분)


def _conn() -> sqlite3.Connection:
    """새 SQLite 연결을 반환한다. 호출자는 반드시 finally에서 close()해야 한다."""
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    """pending_approvals 테이블을 생성(없을 경우)하고 스키마를 마이그레이션한다."""
    with _lock:
        conn = _conn()
        try:
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
                conn.execute(
                    "ALTER TABLE pending_approvals ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # 이미 존재하는 컬럼 — 정상
            conn.commit()
        finally:
            conn.close()


def create_request(command: str, error_log: str, reason: str) -> str:
    """새 승인 요청을 삽입하고 토큰을 반환한다."""
    token      = secrets.token_urlsafe(32)
    now        = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=EXPIRY_MINUTES)).isoformat()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO pending_approvals "
                "(token, command, error_log, reason, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, command, error_log, reason, now.isoformat(), expires_at),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def get_status(token: str) -> str | None:
    """
    토큰의 현재 상태를 반환한다.
    반환값: 'pending' | 'approved' | 'rejected' | 'expired' | None(미존재)
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT status, expires_at FROM pending_approvals WHERE token = ?", (token,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    if row["status"] == "pending" and row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            # 타임존 없는 구형 레코드 방어 — UTC로 간주
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                return "expired"
        except ValueError:
            pass

    return row["status"]


def get_request(token: str) -> dict | None:
    """토큰에 해당하는 요청 전체 정보를 반환한다. 없으면 None."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT command, error_log, status, created_at, expires_at "
            "FROM pending_approvals WHERE token = ?",
            (token,),
        ).fetchone()
    finally:
        conn.close()

    return dict(row) if row is not None else None


def set_decision(token: str, decision: str) -> bool:
    """
    decision: 'approved' 또는 'rejected'.
    상태가 'pending'일 때만 업데이트하며 성공 여부를 반환한다.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE pending_approvals SET status = ?, decided_at = ? "
                "WHERE token = ? AND status = 'pending'",
                (decision, now, token),
            )
            conn.commit()
        finally:
            conn.close()
    return cur.rowcount == 1
