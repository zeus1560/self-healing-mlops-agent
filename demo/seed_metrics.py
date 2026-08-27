"""
발표용 메트릭 시드 데이터 생성기
==================================
최근 7일치 현실적인 에이전트 처리 기록을 agent_metrics.db에 삽입합니다.

실행:
    python demo/seed_metrics.py           # 기본 100건 생성
    python demo/seed_metrics.py --n 200   # 200건 생성
    python demo/seed_metrics.py --clear   # 시드 데이터만 삭제 후 재생성
"""

import argparse
import hashlib
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "agent_metrics.db")
SEED_MARKER = "seed_demo_v1"  # command 컬럼에 마커 삽입 — 삭제 시 식별용

# ── 시나리오 정의 ─────────────────────────────────────────────────────────────
# (error_category, log_snippet, action_type, weight)
SCENARIOS = [
    ("Out_Of_Memory",     "kernel: Out of memory: Kill process api-server score 982",         "CLEAR_MEMORY",       20),
    ("Out_Of_Memory",     "OOM killer invoked for process worker (pid=4821)",                  "RESTART_SERVICE",    10),
    ("Disk_Full",         "No space left on device: /var/log/nginx/access.log",                "CLEAR_MEMORY",       15),
    ("Disk_Full",         "postgres: could not write to pg_wal: No space left on device",      "RESTART_SERVICE",    10),
    ("Process_Crash",     "nginx worker process 3291 exited with signal 11 (SIGSEGV)",         "RESTART_SERVICE",    12),
    ("Process_Crash",     "api-server.service: Main process exited, code=killed status=9/KILL","RESTART_SERVICE",     8),
    ("DB_Connection",     "PostgreSQL Connection Timeout — unable to acquire connection 30s",  "RESTART_SERVICE",    12),
    ("DB_Connection",     "Database connection pool exhausted — all 100 connections in use",   "ESCALATE_TO_HUMAN",   5),
    ("Auth_Error",        "AuthException: token signature verification failed user_id=58291",  "CLEAR_MEMORY",        8),
    ("Auth_Error",        "repeated auth failures — possible credential compromise",           "ESCALATE_TO_HUMAN",   4),
    ("Network_Timeout",   "Network Timeout — connection to 10.0.3.12:443 timed out 5000ms",   "RESTART_SERVICE",     8),
    ("Permission_Denied", "PermissionError [Errno 13] Permission denied: '/etc/nginx/nginx.conf'", "ESCALATE_TO_HUMAN", 4),
    ("Memory_Leak",       "memory leak rate +120MB/min — intervention required",              "CLEAR_MEMORY",        4),
]

WEIGHTS = [s[3] for s in SCENARIOS]

# ── 결과 분포 ─────────────────────────────────────────────────────────────────
# L1 98.8%, L2 1.2% / 실험 결과(threshold τ=0.6, Top-K=5) 기준
# 전체 성공률 ~74%: ESCALATE(13% 비중)는 항상 IMPOSSIBLE,
# 나머지 85% 성공 → 0.87*0.85 ≈ 74%
def _pick_outcome(action_type: str):
    source = random.choices(["L1_CACHE", "L2_LLM"], weights=[988, 12])[0]
    if action_type == "ESCALATE_TO_HUMAN":
        result = "IMPOSSIBLE"
        success = 0
    else:
        result = random.choices(
            ["SUCCESS", "FAILURE", "IMPOSSIBLE"],
            weights=[85, 10, 5]
        )[0]
        success = 1 if result == "SUCCESS" else 0
    return source, result, success

def _latency(source: str) -> float:
    if source == "L1_CACHE":
        # threshold 실험 τ=0.6 기준 avg 30.46ms
        return random.gauss(0.0305, 0.008)
    else:
        return random.gauss(3.8, 1.2)     # ~3800ms (Qwen LLM 추론)

def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")

# ── 생성 ─────────────────────────────────────────────────────────────────────
def generate(n: int = 100) -> list[tuple]:
    now = datetime.now()
    rows = []
    for _ in range(n):
        cat, log, action, _ = random.choices(SCENARIOS, weights=WEIGHTS)[0]
        source, result, success = _pick_outcome(action)
        latency = max(0.010, _latency(source))

        # 최근 7일 내 균등 분포 (더 최근에 약간 더 많이)
        days_ago = random.choices(
            range(7),
            weights=[30, 20, 15, 12, 10, 8, 5]  # 0일 전(오늘) ~ 6일 전
        )[0]
        hour = random.randint(0, 23)
        minute = random.randint(0, 59)
        second = random.randint(0, 59)
        dt = now - timedelta(days=days_ago, hours=now.hour - hour,
                             minutes=now.minute - minute, seconds=now.second - second)
        # 단순하게: 오늘에서 days_ago일 + 랜덤 시각
        base = now.replace(hour=hour, minute=minute, second=second, microsecond=random.randint(0, 999999))
        dt = base - timedelta(days=days_ago)

        error_detail = SEED_MARKER if result == "SUCCESS" else f"복구 시도 중 예외 발생: {action} 실패 [{SEED_MARKER}]"
        error_type   = cat

        rows.append((
            _ts(dt),          # timestamp
            log,              # error_log
            source,           # resolution_source
            action,           # action_type
            latency,          # latency_sec
            result,           # result_category
            success,          # success
            cat,              # error_category
            error_type,       # error_type
            error_detail,     # error_detail
        ))

    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def insert(rows: list[tuple], db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # 컬럼 목록 확인 후 삽입
    cols = [r[1] for r in cur.execute("PRAGMA table_info(metrics)").fetchall()]
    needed = ["timestamp", "error_log", "resolution_source", "action_type",
              "latency_sec", "result_category", "success",
              "error_category", "error_type", "error_detail"]
    placeholders = ", ".join("?" * len(needed))
    col_str = ", ".join(needed)
    cur.executemany(
        f"INSERT INTO metrics ({col_str}) VALUES ({placeholders})",
        rows
    )
    conn.commit()
    inserted = cur.rowcount
    conn.close()
    return len(rows)


def clear_seed(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM metrics WHERE error_detail LIKE ?", (f"%{SEED_MARKER}%",))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="발표용 메트릭 시드 데이터 생성")
    parser.add_argument("--n",     type=int, default=100, help="생성할 레코드 수 (기본: 100)")
    parser.add_argument("--clear", action="store_true",   help="시드 데이터 삭제 후 재생성")
    parser.add_argument("--only-clear", action="store_true", help="시드 데이터만 삭제하고 종료")
    parser.add_argument("--reset-all", action="store_true", help="metrics 테이블 전체 초기화 후 재생성")
    args = parser.parse_args()

    db = os.path.abspath(DB_PATH)
    if not os.path.exists(db):
        print(f"[오류] DB 파일 없음: {db}")
        sys.exit(1)

    if args.only_clear:
        deleted = clear_seed(db)
        print(f"✓ 시드 데이터 {deleted}건 삭제 완료")
        return

    if args.reset_all:
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM metrics")
        conn.commit()
        conn.close()
        print("✓ metrics 테이블 전체 초기화 완료")

    elif args.clear:
        deleted = clear_seed(db)
        print(f"  기존 시드 {deleted}건 삭제")

    rows = generate(args.n)
    inserted = insert(rows, db)
    print(f"✓ {inserted}건 삽입 완료 (최근 7일치, L1/L2 혼합)")

    # 삽입 후 현황 출력
    conn = sqlite3.connect(db)
    total   = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    s_rate  = conn.execute("SELECT AVG(success) FROM metrics").fetchone()[0]
    by_src  = conn.execute("SELECT resolution_source, COUNT(*) FROM metrics GROUP BY resolution_source").fetchall()
    conn.close()
    print(f"  DB 총 레코드: {total}건 | 전체 성공률: {s_rate*100:.1f}%")
    print(f"  소스별: {dict(by_src)}")


if __name__ == "__main__":
    main()
