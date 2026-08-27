"""
Proactive Monitor — 에러 발생 전 시스템 임계치를 감시하고 선제적으로 파이프라인을 발동.

감시 항목:
  CPU    >= 90% 연속 2회  → "CRITICAL CPU usage" 합성 로그 발동
  Memory >= 85%           → "CRITICAL Memory usage" 합성 로그 발동
  Disk   >= 90%           → "CRITICAL Disk usage" 합성 로그 발동
  VRAM   >= 90%           → "CRITICAL VRAM usage" 합성 로그 발동 (Intel Arc GPU)

check_and_trigger() 를 메인 루프에서 주기적으로 호출하면 된다.
파이프라인 콜백 없이도 단독으로 경고 로그를 남긴다.
"""

import logging
import os
import time
import traceback

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False
    logging.debug("[ProactiveMonitor] psutil 미설치 — CPU/Memory/Disk 모니터링 비활성화")

try:
    from src.monitor.vram_profiler import get_intel_gpu_stats as _get_gpu_stats
    _VRAM_OK = True
except ImportError:
    _VRAM_OK = False

CPU_THRESHOLD_PCT   = float(os.getenv("PROACTIVE_CPU_THRESHOLD_PCT",  "90.0"))
MEM_THRESHOLD_PCT   = float(os.getenv("PROACTIVE_MEM_THRESHOLD_PCT",  "85.0"))
DISK_THRESHOLD_PCT  = float(os.getenv("PROACTIVE_DISK_THRESHOLD_PCT", "90.0"))
VRAM_THRESHOLD_PCT  = float(os.getenv("PROACTIVE_VRAM_THRESHOLD_PCT", "90.0"))
CHECK_INTERVAL_SEC  = int(os.getenv("PROACTIVE_CHECK_INTERVAL_SEC",   "60"))
CPU_STREAK_TRIGGER  = int(os.getenv("PROACTIVE_CPU_STREAK_TRIGGER",   "2"))
ALERT_COOLDOWN_SEC  = int(os.getenv("PROACTIVE_ALERT_COOLDOWN_SEC",   "1800"))


class ProactiveMonitor:
    """
    시스템 리소스를 주기적으로 점검해 임계치 초과 시
    합성(synthetic) 에러 로그를 생성하고 콜백으로 파이프라인을 선제 발동한다.

    메모리 설계:
      _last_fired: dict[str, float] — 알림 키 4개로 크기 고정, 무한 증가 없음.
      _cpu_streak: int — 정수 1개.
      이전 구현의 _triggered set은 매 사이클 clear() 했으나
      그로 인해 임계치가 지속되는 동안 매 60초마다 알림이 반복 발동되는 버그 존재.
      ALERT_COOLDOWN_SEC(30분) 기반 쿨다운으로 교체해 알림 폭풍을 방지한다.
    """

    def __init__(self, pipeline_callback=None):
        """
        pipeline_callback : (error_log: str) -> None
            log_watcher.LogTailHandler.trigger_agent_pipeline 과 동일 시그니처.
            None 이면 경고 로그만 출력한다.
        """
        self._callback   = pipeline_callback
        self._cpu_streak = 0
        self._last_check = 0.0
        self._last_fired: dict[str, float] = {}  # key → 마지막 발동 시각, 최대 3개 항목

    # ── 공개 인터페이스 ────────────────────────────────────────────────────
    def check_and_trigger(self) -> None:
        """메인 루프에서 호출. CHECK_INTERVAL_SEC 미만이면 즉시 반환."""
        now = time.time()
        if now - self._last_check < CHECK_INTERVAL_SEC:
            return
        self._last_check = now

        try:
            if _PSUTIL_OK:
                self._check_cpu()
                self._check_memory()
                self._check_disk()
            self._check_vram()
        except Exception:
            logging.error(f"[ProactiveMonitor] 점검 중 오류:\n{traceback.format_exc()}")

    # ── 내부 점검 로직 ─────────────────────────────────────────────────────
    def _check_cpu(self) -> None:
        cpu = psutil.cpu_percent(interval=1)
        if cpu >= CPU_THRESHOLD_PCT:
            self._cpu_streak += 1
            logging.warning(
                f"[ProactiveMonitor] CPU {cpu:.1f}% ≥ {CPU_THRESHOLD_PCT}% "
                f"(연속 {self._cpu_streak}/{CPU_STREAK_TRIGGER}회)"
            )
            if self._cpu_streak >= CPU_STREAK_TRIGGER:
                self._fire(
                    f"CRITICAL CPU usage {cpu:.1f}% — potential OOM or runaway process detected proactively",
                    key="cpu",
                )
                self._cpu_streak = 0
        else:
            if self._cpu_streak > 0:
                logging.info(f"[ProactiveMonitor] CPU 정상화 ({cpu:.1f}%)")
            self._cpu_streak = 0

    def _check_memory(self) -> None:
        mem = psutil.virtual_memory()
        pct = mem.percent
        if pct >= MEM_THRESHOLD_PCT:
            avail_mb = mem.available // (1024 * 1024)
            logging.warning(f"[ProactiveMonitor] Memory {pct:.1f}% ≥ {MEM_THRESHOLD_PCT}% (가용: {avail_mb}MB)")
            self._fire(
                f"CRITICAL Memory usage {pct:.1f}% — OOM risk detected proactively (available: {avail_mb}MB)",
                key="memory",
            )

    def _check_disk(self) -> None:
        usage = psutil.disk_usage("/")
        pct   = usage.percent
        if pct >= DISK_THRESHOLD_PCT:
            free_gb = usage.free // (1024 ** 3)
            logging.warning(f"[ProactiveMonitor] Disk {pct:.1f}% ≥ {DISK_THRESHOLD_PCT}% (여유: {free_gb}GB)")
            self._fire(
                f"CRITICAL Disk usage {pct:.1f}% — no space left on device risk (free: {free_gb}GB)",
                key="disk",
            )

    def _check_vram(self) -> None:
        if not _VRAM_OK:
            return
        stats = _get_gpu_stats()
        used  = stats.get("vram_used_mb")
        total = stats.get("vram_total_mb")
        if used is None or total is None or total == 0:
            return
        pct = used / total * 100
        if pct >= VRAM_THRESHOLD_PCT:
            logging.warning(
                f"[ProactiveMonitor] VRAM {pct:.1f}% ≥ {VRAM_THRESHOLD_PCT}% "
                f"({used:.0f}/{total:.0f} MiB)"
            )
            self._fire(
                f"CRITICAL VRAM usage {pct:.1f}% — GPU OOM risk for IPEX-LLM "
                f"({used:.0f}/{total:.0f} MiB) detected proactively",
                key="vram",
            )

    def _fire(self, synthetic_log: str, key: str) -> None:
        """ALERT_COOLDOWN_SEC 이내 동일 알림 재발동 방지 후 콜백 실행."""
        now      = time.time()
        last     = self._last_fired.get(key, 0.0)
        cooldown = ALERT_COOLDOWN_SEC
        if now - last < cooldown:
            remaining = int(cooldown - (now - last))
            logging.debug(
                f"[ProactiveMonitor] '{key}' 알림 쿨다운 중 (남은 시간: {remaining//60}분)"
            )
            return
        self._last_fired[key] = now

        logging.warning(f"[ProactiveMonitor] 선제 파이프라인 발동: {synthetic_log}")
        if self._callback:
            try:
                self._callback(synthetic_log)
            except Exception:
                logging.error(f"[ProactiveMonitor] 콜백 실행 실패:\n{traceback.format_exc()}")
