"""
Circuit Breaker — 동일 에러 연속 실패 시 LLM 우회 + Slack 에스컬레이션.

상태 머신:
  CLOSED    → 정상 동작. 연속 실패가 FAILURE_THRESHOLD 에 도달하면 OPEN으로 전환.
  OPEN      → 해당 에러 처리 차단. OPEN_TIMEOUT_SEC(30분) 경과 후 HALF_OPEN으로 전환.
  HALF_OPEN → 시험 요청 1회 허용. 성공 시 CLOSED, 실패 시 OPEN 재진입.

동일 에러 판별: error_log 앞 100자 정규화 후 MD5 해시.
상태 저장: agent_metrics.db 내 circuit_breaker 테이블.

개선:
  - sqlite_pool로 스레드당 연결 재사용 (#7)
  - test_in_progress 컬럼으로 HALF_OPEN 시험 요청을 원자적으로 1회만 허용 (#10)
    → 인메모리 _half_open_sigs 제거: 프로세스 재시작 후에도 경합 없음
"""

import hashlib
import json
import logging
import os
import traceback
import urllib.request
from datetime import datetime, timezone

from src.utils.sqlite_pool import get_conn

FAILURE_THRESHOLD = 3
OPEN_TIMEOUT_SEC  = 30 * 60
_SIG_CHARS        = 100

STATE_CLOSED    = "CLOSED"
STATE_OPEN      = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, db_path: str = "./data/agent_metrics.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path    = db_path
        self._slack_url = os.getenv("SLACK_WEBHOOK_URL")
        self._init_table()
        logging.info("[CircuitBreaker] 초기화 완료.")

    # ── 초기화 ─────────────────────────────────────────────────────────
    def _init_table(self) -> None:
        conn = get_conn(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS circuit_breaker (
                error_sig            TEXT PRIMARY KEY,
                state                TEXT    NOT NULL DEFAULT 'CLOSED',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                opened_at            TEXT,
                last_updated         TEXT    NOT NULL,
                test_in_progress     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # 기존 테이블에 test_in_progress 컬럼이 없으면 추가 (마이그레이션)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(circuit_breaker)")}
        if "test_in_progress" not in existing:
            conn.execute("ALTER TABLE circuit_breaker ADD COLUMN test_in_progress INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # ── 서명 추출 ───────────────────────────────────────────────────────
    @staticmethod
    def _sig(error_log: str) -> str:
        first_line = error_log.splitlines()[0] if error_log else error_log
        normalized = " ".join(first_line[:_SIG_CHARS].lower().split())
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    # ── DB I/O ──────────────────────────────────────────────────────────
    def _read(self, sig: str) -> dict:
        conn = get_conn(self.db_path)
        row  = conn.execute(
            "SELECT * FROM circuit_breaker WHERE error_sig = ?", (sig,)
        ).fetchone()
        if row is None:
            return {"state": STATE_CLOSED, "consecutive_failures": 0,
                    "opened_at": None, "test_in_progress": 0}
        return dict(row)

    def _write(self, sig: str, state: str, failures: int,
               opened_at: str | None, test_in_progress: int = 0) -> None:
        now  = datetime.now(timezone.utc).isoformat()
        conn = get_conn(self.db_path)
        conn.execute("""
            INSERT INTO circuit_breaker
                (error_sig, state, consecutive_failures, opened_at, last_updated, test_in_progress)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(error_sig) DO UPDATE SET
                state                = excluded.state,
                consecutive_failures = excluded.consecutive_failures,
                opened_at            = excluded.opened_at,
                last_updated         = excluded.last_updated,
                test_in_progress     = excluded.test_in_progress
        """, (sig, state, failures, opened_at, now, test_in_progress))
        conn.commit()

    # ── 공개 인터페이스 ─────────────────────────────────────────────────
    def can_proceed(self, error_log: str) -> bool:
        """
        True  → 파이프라인 정상 진행 (CLOSED / HALF_OPEN 시험 첫 요청)
        False → 차단 (OPEN 또는 HALF_OPEN 중복 요청)
        """
        sig   = self._sig(error_log)
        data  = self._read(sig)
        state = data["state"]

        if state == STATE_CLOSED:
            return True

        if state == STATE_OPEN:
            opened_at = data.get("opened_at")
            if opened_at is None:
                return False
            elapsed = (
                datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)
            ).total_seconds()

            if elapsed >= OPEN_TIMEOUT_SEC:
                # OPEN → HALF_OPEN 전환 후 시험 요청 원자적으로 허용
                self._write(sig, STATE_HALF_OPEN, data["consecutive_failures"],
                            opened_at, test_in_progress=0)
                return self._try_claim_half_open(sig, elapsed)

            logging.warning(
                f"[CircuitBreaker] OPEN 차단 (남은 시간: "
                f"{(OPEN_TIMEOUT_SEC - elapsed)/60:.1f}분): {sig[:8]}"
            )
            return False

        if state == STATE_HALF_OPEN:
            return self._try_claim_half_open(sig, elapsed=None)

        return True  # 알 수 없는 상태 → 허용 (보수적)

    def _try_claim_half_open(self, sig: str, elapsed) -> bool:
        """
        test_in_progress = 0 → 1 원자적 업데이트.
        1행 업데이트 성공 시 이 요청이 유일한 시험 요청.
        0행이면 이미 다른 요청이 진행 중 → 차단.
        재시작 후에도 DB 기반이므로 인메모리 상태가 필요 없다.
        """
        conn = get_conn(self.db_path)
        cur  = conn.execute(
            "UPDATE circuit_breaker SET test_in_progress = 1 "
            "WHERE error_sig = ? AND test_in_progress = 0",
            (sig,)
        )
        conn.commit()
        if cur.rowcount == 1:
            if elapsed is not None:
                logging.warning(
                    f"[CircuitBreaker] OPEN→HALF_OPEN ({elapsed/60:.1f}분 경과). "
                    f"시험 요청 허용: {sig[:8]}"
                )
            else:
                logging.warning(f"[CircuitBreaker] HALF_OPEN 시험 요청 허용: {sig[:8]}")
            return True
        logging.warning(f"[CircuitBreaker] HALF_OPEN 중 추가 요청 차단: {sig[:8]}")
        return False

    def record_result(self, error_log: str, success: bool) -> None:
        """파이프라인 결과 기록. 상태 전이 및 에스컬레이션 처리."""
        sig   = self._sig(error_log)
        data  = self._read(sig)
        state = data["state"]

        if success:
            if state in (STATE_HALF_OPEN, STATE_OPEN):
                logging.info(f"[CircuitBreaker] 복구 성공 → CLOSED: {sig[:8]}")
            self._write(sig, STATE_CLOSED, 0, None, test_in_progress=0)
            return

        failures = data["consecutive_failures"] + 1

        if state == STATE_HALF_OPEN:
            opened_at = datetime.now(timezone.utc).isoformat()
            self._write(sig, STATE_OPEN, failures, opened_at, test_in_progress=0)
            logging.warning(f"[CircuitBreaker] HALF_OPEN 시험 실패 → OPEN 재진입: {sig[:8]}")
            self._send_escalation(error_log, failures, reopened=True)
            return

        if failures >= FAILURE_THRESHOLD:
            opened_at = datetime.now(timezone.utc).isoformat()
            self._write(sig, STATE_OPEN, failures, opened_at, test_in_progress=0)
            logging.error(
                f"[CircuitBreaker] 연속 실패 {failures}회 → OPEN. "
                f"30분간 LLM 우회: {sig[:8]}"
            )
            self._send_escalation(error_log, failures, reopened=False)
        else:
            self._write(sig, STATE_CLOSED, failures, data.get("opened_at"), test_in_progress=0)
            logging.warning(f"[CircuitBreaker] 실패 누적 {failures}/{FAILURE_THRESHOLD}: {sig[:8]}")

    def get_status(self, error_log: str) -> dict:
        sig  = self._sig(error_log)
        data = self._read(sig)
        return {
            "sig":              sig[:8],
            "state":            data["state"],
            "failures":         data["consecutive_failures"],
            "opened_at":        data.get("opened_at"),
            "test_in_progress": data.get("test_in_progress", 0),
        }

    # ── Slack 에스컬레이션 ──────────────────────────────────────────────
    def _send_escalation(self, error_log: str, failures: int, reopened: bool) -> None:
        label   = "OPEN 재진입" if reopened else "Circuit OPEN"
        message = (
            f"🔴 *[Self-Healing Agent: Circuit Breaker {label}]*\n"
            f"• *에러 요약*: `{error_log[:150]}`\n"
            f"• *연속 실패 횟수*: {failures}회\n"
            f"• *조치*: 30분간 LLM 자동 조치 중단, 인간 에스컬레이션\n"
            f"⚠️ *관리자 직접 확인이 필요합니다!*"
        )
        if not self._slack_url:
            logging.warning(f"[CircuitBreaker] Slack URL 없음. 콘솔 에스컬레이션:\n{message}")
            return
        try:
            payload = json.dumps({"text": message}).encode("utf-8")
            req = urllib.request.Request(
                self._slack_url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            logging.info("[CircuitBreaker] Slack 에스컬레이션 전송 완료.")
        except Exception:
            logging.error(f"[CircuitBreaker] Slack 전송 실패:\n{traceback.format_exc()}")
