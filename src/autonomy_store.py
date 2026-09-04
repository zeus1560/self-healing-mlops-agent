"""
카테고리별 Progressive Autonomy 레벨을 SQLite에 저장·조회·변경하는 스토어.

executor.py의 게이트, scripts/set_autonomy_level.py(수동 승급 CLI),
experiments/run_shadow_gate_report.py(승급 판정 리포트)가 공유한다.

설계 원칙 (2026-09-03 /grill-me 세션 결정):
  승급/강등은 절대 코드에서 자동으로 일어나지 않는다 — set_level()은
  scripts/set_autonomy_level.py를 통해 사람이 직접 호출할 때만 실행된다.

thread-safety: approval_store.py와 동일한 패턴(쓰기는 _lock으로 직렬화).
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

from src.schemas import AutonomyLevel

_DB_PATH = "./data/agent_metrics.db"
_lock    = threading.Lock()

# 테이블에 레벨이 아예 없는 카테고리(신규 배포 직후 등)의 기본값.
# 2026-09-04 결정: 모든 카테고리를 보수적으로 시작 — AUTO를 기본값으로 두지 않는다.
DEFAULT_AUTONOMY_LEVEL = AutonomyLevel(
    os.getenv("DEFAULT_AUTONOMY_LEVEL", AutonomyLevel.APPROVE_THEN_EXECUTE.value)
)


def _conn() -> sqlite3.Connection:
    """새 SQLite 연결을 반환한다. 호출자는 반드시 finally에서 close()해야 한다."""
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    """autonomy_state·shadow_events 테이블을 생성한다(없을 경우)."""
    with _lock:
        conn = _conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS autonomy_state (
                    category            TEXT PRIMARY KEY,
                    level               TEXT NOT NULL,
                    updated_at          TEXT NOT NULL,
                    updated_by          TEXT,
                    shadow_target_level TEXT,
                    shadow_started_at   TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    category   TEXT NOT NULL,
                    timestamp  TEXT NOT NULL,
                    from_level TEXT NOT NULL,
                    to_level   TEXT NOT NULL,
                    event      TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()


def get_level(category: str) -> AutonomyLevel:
    """카테고리의 현재 레벨을 반환한다. 저장된 값이 없으면 DEFAULT_AUTONOMY_LEVEL."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT level FROM autonomy_state WHERE category = ?", (category,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return DEFAULT_AUTONOMY_LEVEL
    try:
        return AutonomyLevel(row["level"])
    except ValueError:
        return DEFAULT_AUTONOMY_LEVEL


def set_level(category: str, level: AutonomyLevel, updated_by: str, note: str = "") -> None:
    """카테고리의 레벨을 수동으로 변경한다. shadow_events에 이벤트를 기록한다."""
    prev = get_level(category)
    now  = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO autonomy_state (category, level, updated_at, updated_by,
                                             shadow_target_level, shadow_started_at)
                VALUES (?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(category) DO UPDATE SET
                    level = excluded.level,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by,
                    shadow_target_level = NULL,
                    shadow_started_at = NULL
                """,
                (category, level.value, now, updated_by),
            )
            event = "promoted" if _level_rank(level) > _level_rank(prev) else "demoted"
            conn.execute(
                "INSERT INTO shadow_events (category, timestamp, from_level, to_level, event) "
                "VALUES (?, ?, ?, ?, ?)",
                (category, now, prev.value, level.value, event if prev != level else "unchanged"),
            )
            conn.commit()
        finally:
            conn.close()


def start_shadow(category: str, target_level: AutonomyLevel, updated_by: str = "") -> None:
    """카테고리를 target_level로 승급 검토 중 상태로 표시한다(실제 레벨은 바꾸지 않음)."""
    current = get_level(category)
    now     = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO autonomy_state (category, level, updated_at, updated_by,
                                             shadow_target_level, shadow_started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET
                    shadow_target_level = excluded.shadow_target_level,
                    shadow_started_at = excluded.shadow_started_at
                """,
                (category, current.value, now, updated_by, target_level.value, now),
            )
            conn.execute(
                "INSERT INTO shadow_events (category, timestamp, from_level, to_level, event) "
                "VALUES (?, ?, ?, ?, 'shadow_started')",
                (category, now, current.value, target_level.value),
            )
            conn.commit()
        finally:
            conn.close()


def get_shadow_target(category: str) -> AutonomyLevel | None:
    """카테고리가 승급 검토 중이면 목표 레벨을, 아니면 None을 반환한다."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT shadow_target_level FROM autonomy_state WHERE category = ?", (category,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["shadow_target_level"]:
        return None
    try:
        return AutonomyLevel(row["shadow_target_level"])
    except ValueError:
        return None


def list_all() -> list[dict]:
    """모든 카테고리의 현재 레벨·Shadow 상태를 반환한다(대시보드/CLI용)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT category, level, updated_at, updated_by, "
            "shadow_target_level, shadow_started_at FROM autonomy_state ORDER BY category"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


_LEVEL_ORDER = [
    AutonomyLevel.READ_ONLY,
    AutonomyLevel.PROPOSE,
    AutonomyLevel.APPROVE_THEN_EXECUTE,
    AutonomyLevel.AUTO,
]


def _level_rank(level: AutonomyLevel) -> int:
    return _LEVEL_ORDER.index(level)
