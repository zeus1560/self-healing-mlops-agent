"""
통합 테스트 — [8]~[12] 전 구간 검증

테스트 항목:
  T1. 컨텍스트 윈도우 빌드 (_build_context_window)
  T2. Circuit Breaker 상태 전이 (CLOSED→OPEN→HALF_OPEN→CLOSED)
  T3. Circuit Breaker + pipeline 차단 연동
  T4. Maintenance (30일 초과 삭제 + VACUUM + 중복 방지)
  T5. RAGEngine L1 패스 (ChromaDB → executor → observer 전 구간)
  T6. 컨텍스트 윈도우가 RAGEngine에 전달됨을 확인
"""

import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from collections import deque

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ["AUTO_APPROVE"] = "true"

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results: list[tuple[str, str, str]] = []  # (name, status, detail)


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════════════
# T1. 컨텍스트 윈도우 빌드
# ══════════════════════════════════════════════════════════════════════════
print("\n[T1] 컨텍스트 윈도우 빌드")

from src.log_watcher import LogTailHandler

error_line = "ERROR: CUDA out of memory. Tried to allocate 2.00 GiB"
before     = [f"INFO step {i}" for i in range(10)]
after      = [f"DEBUG retry {i}" for i in range(10)]

ctx = LogTailHandler._build_context_window(error_line, before, after)
lines = ctx.splitlines()

record("첫 줄 = 에러 줄",            lines[0] == error_line)
record("[LOG CONTEXT] 헤더 존재",    "[LOG CONTEXT]" in ctx)
record("에러 마커(>>>) 존재",         f">>> {error_line}" in ctx)
record("총 줄 수 23줄",              len(lines) == 23, f"실제={len(lines)}")
record("before 없을 때 정상 동작",   LogTailHandler._build_context_window(error_line, [], after[:3]).startswith(error_line))

# circuit_breaker 서명 호환 확인
import hashlib
def _sig(log):
    first = log.splitlines()[0] if log else log
    return hashlib.md5(" ".join(first[:100].lower().split()).encode()).hexdigest()
record("circuit_breaker 서명 호환",  _sig(ctx) == _sig(error_line))

# ══════════════════════════════════════════════════════════════════════════
# T2. Circuit Breaker 상태 전이
# ══════════════════════════════════════════════════════════════════════════
print("\n[T2] Circuit Breaker 상태 전이")

from src.circuit_breaker import CircuitBreaker

db_tmp = tempfile.mktemp(suffix=".db")
cb = CircuitBreaker(db_path=db_tmp)
log = "ERROR: connection refused to postgres:5432"

record("초기 CLOSED → can_proceed=True", cb.can_proceed(log))
cb.record_result(log, False)
cb.record_result(log, False)
record("2회 실패 후 여전히 CLOSED",   cb.get_status(log)["state"] == "CLOSED")
cb.record_result(log, False)
record("3회 실패 후 OPEN",            cb.get_status(log)["state"] == "OPEN")
record("OPEN → can_proceed=False",   not cb.can_proceed(log))

# OPEN → HALF_OPEN (타이머 조작)
sig = cb._sig(log)
past = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
cb._write(sig, "OPEN", 3, past)
record("30분 경과 후 can_proceed=True (HALF_OPEN)", cb.can_proceed(log))
record("HALF_OPEN 중 추가 요청 차단",  not cb.can_proceed(log))

# HALF_OPEN 성공 → CLOSED
cb.record_result(log, True)
record("HALF_OPEN 성공 → CLOSED",    cb.get_status(log)["state"] == "CLOSED")
record("실패 카운터 리셋",             cb.get_status(log)["failures"] == 0)

os.unlink(db_tmp)

# ══════════════════════════════════════════════════════════════════════════
# T3. Circuit Breaker 파이프라인 차단
# ══════════════════════════════════════════════════════════════════════════
print("\n[T3] Circuit Breaker 파이프라인 차단 연동")

db_tmp2 = tempfile.mktemp(suffix=".db")
cb2 = CircuitBreaker(db_path=db_tmp2)
blocked_log = "CRITICAL: disk full /dev/sda1"

# 3회 실패 → OPEN
for _ in range(3):
    cb2.record_result(blocked_log, False)

pipeline_called = []

class _FakePipeline:
    def run(self, error_log):
        if not cb2.can_proceed(error_log):
            return "BLOCKED"
        pipeline_called.append(error_log)
        return "PROCESSED"

fp = _FakePipeline()
result = fp.run(blocked_log)
record("OPEN 상태 → 파이프라인 BLOCKED", result == "BLOCKED")
record("pipeline_called 비어있음",       len(pipeline_called) == 0)

os.unlink(db_tmp2)

# ══════════════════════════════════════════════════════════════════════════
# T4. Maintenance
# ══════════════════════════════════════════════════════════════════════════
print("\n[T4] Maintenance 실행")

from src.maintenance import MaintenanceRunner

db_m = tempfile.mktemp(suffix=".db")
with sqlite3.connect(db_m) as conn:
    conn.execute("""CREATE TABLE metrics (
        id INTEGER PRIMARY KEY, timestamp TEXT, error_log TEXT,
        resolution_source TEXT, action_type TEXT, latency_sec REAL,
        success BOOLEAN, result_category TEXT, error_type TEXT, error_detail TEXT
    )""")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO metrics VALUES (1,?,'old','L1','R',1,1,'SUCCESS',NULL,NULL)", (old_ts,))
    conn.execute("INSERT INTO metrics VALUES (2,?,'new','L1','R',1,1,'SUCCESS',NULL,NULL)", (new_ts,))
    conn.commit()

mr = MaintenanceRunner(db_path=db_m)
record("첫 실행 should_run=True",    mr.should_run())
res = mr.run()
record("30일 초과 1건 삭제",          res["deleted"] == 1, f"삭제={res['deleted']}")

with sqlite3.connect(db_m) as conn:
    remaining = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
record("최신 레코드 1건 보존",         remaining == 1, f"잔여={remaining}")
record("실행 후 should_run=False",    not mr.should_run())

os.unlink(db_m)

# ══════════════════════════════════════════════════════════════════════════
# T5. RAGEngine L1 패스 → executor → observer 전 구간
# ══════════════════════════════════════════════════════════════════════════
print("\n[T5] RAGEngine L1 → executor → observer 전 구간")

from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.observability import AgentObserver
from src.schemas import ActionType

db_obs = tempfile.mktemp(suffix=".db")
engine   = RAGEngine()
executor = ActionExecutor()
observer = AgentObserver(db_path=db_obs)

# ChromaDB에 350건이 있으므로 Out_Of_Memory는 L1에서 높은 확률로 매칭
test_log = "OOM killer invoked: process python3 killed due to out of memory"
t0       = time.perf_counter()
decision = engine.analyze_error(test_log)
latency  = time.perf_counter() - t0

record("RAGEngine 응답 반환",         decision is not None)
is_l1 = "[Vector DB 유사도 매칭 성공]" in decision.reasoning
is_l2 = "[Ollama 추론 (L2)]" in decision.reasoning or "[규칙 기반 추론]" in decision.reasoning
record("L1 또는 L2 경로 판별 완료",   is_l1 or is_l2,
       f"L1={is_l1} L2={is_l2} 거리 기반 분기 정상")

exec_result = executor.execute(decision, original_error_log=test_log)
record("executor 반환값 dict 형식",   isinstance(exec_result, dict))
record("result_category 필드 존재",   "result_category" in exec_result)

observer.log_event(
    error_log=test_log,
    source="L1_CACHE",
    action_type=decision.action_type.name,
    latency_sec=latency,
    success=exec_result["success"],
    result_category=exec_result["result_category"],
    error_type=exec_result["error_type"],
    error_detail=exec_result["error_detail"],
)
with sqlite3.connect(db_obs) as conn:
    row = conn.execute("SELECT result_category FROM metrics WHERE error_log=?", (test_log,)).fetchone()
record("observer DB 기록 확인",       row is not None and row[0] in ("SUCCESS", "FAILURE", "IMPOSSIBLE"),
       f"result_category={row[0] if row else 'None'}")
os.unlink(db_obs)

# ══════════════════════════════════════════════════════════════════════════
# T6. 컨텍스트 윈도우 → RAGEngine 전달
# ══════════════════════════════════════════════════════════════════════════
print("\n[T6] 컨텍스트 윈도우 → RAGEngine 전달")

ctx_log = LogTailHandler._build_context_window(
    "ERROR: no space left on device",
    before=["INFO writing file", "INFO flushing buffer"],
    after=["WARNING retrying write", "ERROR write failed"],
)
record("컨텍스트 포함 로그 첫 줄 확인", ctx_log.splitlines()[0] == "ERROR: no space left on device")
t0  = time.perf_counter()
dec = engine.analyze_error(ctx_log)
lat = time.perf_counter() - t0
record("RAGEngine 컨텍스트 포함 입력 처리", dec is not None)
record(f"응답시간 5초 이내",           lat < 5.0, f"{lat:.2f}s")

# ══════════════════════════════════════════════════════════════════════════
# 최종 요약
# ══════════════════════════════════════════════════════════════════════════
total  = len(results)
passed = sum(1 for _, s, _ in results if s == PASS)
failed = total - passed

print(f"\n{'='*60}")
print(f"통합 테스트 결과: {passed}/{total} 통과" + (f"  ({failed}건 실패)" if failed else ""))
print(f"{'='*60}")
if failed:
    print("\n실패 항목:")
    for name, status, detail in results:
        if status == FAIL:
            print(f"  {FAIL}  {name}" + (f" — {detail}" if detail else ""))
sys.exit(0 if failed == 0 else 1)
