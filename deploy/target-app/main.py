from fastapi import FastAPI, BackgroundTasks
import threading, time, os, subprocess
from datetime import datetime

app = FastAPI(title="Target App (Simulated)")

log_path = "/app/data/target_app.log" if os.path.isdir('/app/data') else "./data/target_app.log"
EVIDENCE_LOG = "/app/data/realtime_system.log" if os.path.isdir('/app/data') else "./data/realtime_system.log"

os.makedirs(os.path.dirname(log_path), exist_ok=True)

# 동시에 두 개의 장애가 같은 컨테이너 안에서 겹치지 않도록 하는 락
_injection_lock = threading.Lock()

# background writer to simulate normal traffic
_stop_event = threading.Event()


def _writer():
    i = 0
    while not _stop_event.is_set():
        with open(log_path, "a", encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()} INFO api: request handled id={i}\n")
        i += 1
        time.sleep(2)


threading.Thread(target=_writer, daemon=True).start()


def _append_evidence(level: str, message: str) -> None:
    """에이전트(log_watcher)가 실제로 tail하는 realtime_system.log에 실측 증거를 기록."""
    with open(EVIDENCE_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()} {level} chaos-injector: {message}\n")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/inject/oom")
async def inject_oom():
    """cgroup 메모리 한도(512m)를 초과 요청해 실제 OOM Killer를 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "oom", "skipped": "another injection in progress"}
    try:
        try:
            result = subprocess.run(
                ["stress-ng", "--vm", "1", "--vm-bytes", "700M", "--vm-keep", "--timeout", "20s"],
                capture_output=True, text=True, timeout=25,
            )
            stderr_tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
            _append_evidence(
                "CRITICAL",
                f"stress-ng --vm-bytes=700M against cgroup mem_limit=512m — returncode={result.returncode}, "
                f"real memory-pressure OOM event ({stderr_tail[0]})",
            )
        except subprocess.TimeoutExpired:
            _append_evidence("CRITICAL", "stress-ng OOM test timed out (700M vs 512m limit) — process likely killed by cgroup OOM killer")
        return {"injected": "oom"}
    finally:
        _injection_lock.release()


@app.post("/inject/cpu")
async def inject_cpu():
    """cpus 한도(1.0) 대비 2개 워커로 실제 CPU 포화 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "cpu", "skipped": "another injection in progress"}
    try:
        start = time.time()
        result = subprocess.run(
            ["stress-ng", "--cpu", "2", "--timeout", "15s"],
            capture_output=True, text=True, timeout=20,
        )
        elapsed = time.time() - start
        _append_evidence(
            "CRITICAL",
            f"stress-ng --cpu=2 against cpus=1.0 limit for {elapsed:.1f}s — returncode={result.returncode}, "
            f"sustained CPU saturation detected",
        )
        return {"injected": "cpu"}
    finally:
        _injection_lock.release()


@app.post("/inject/diskfull")
async def inject_diskfull():
    """150m tmpfs(/fill) 용량을 초과 기록해 실제 ENOSPC 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "diskfull", "skipped": "another injection in progress"}
    junk_path = "/fill/junk"
    try:
        result = subprocess.run(
            ["dd", "if=/dev/zero", f"of={junk_path}", "bs=1M", "count=180"],
            capture_output=True, text=True, timeout=30,
        )
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        _append_evidence(
            "ERROR",
            f"dd wrote into 150m tmpfs (/fill) requesting 180M — returncode={result.returncode}, "
            f"real No space left on device ({stderr_tail[0]})",
        )
        return {"injected": "diskfull"}
    finally:
        try:
            if os.path.exists(junk_path):
                os.remove(junk_path)
        finally:
            _injection_lock.release()


@app.post("/inject/process_crash")
async def inject_process_crash(background_tasks: BackgroundTasks):
    """실제 프로세스를 SIGKILL — 컨테이너는 restart:unless-stopped로 자동 복구됨."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "process_crash", "skipped": "another injection in progress"}

    def task():
        try:
            pid = os.getpid()
            try:
                import psutil
                p = psutil.Process(pid)
                rss_mb = p.memory_info().rss / (1024 * 1024)
                uptime_s = time.time() - p.create_time()
                _append_evidence(
                    "CRITICAL",
                    f"target-app process (pid={pid}, rss={rss_mb:.1f}MB, uptime={uptime_s:.1f}s) "
                    f"about to crash — real process crash injection",
                )
            except Exception:
                _append_evidence("CRITICAL", f"target-app process (pid={pid}) about to crash — real process crash injection")
            time.sleep(0.3)
        finally:
            # PID 1 in a container's own PID namespace is immune to signals it sends
            # itself (including SIGKILL) — os.kill() here would silently do nothing.
            # os._exit() bypasses that: it's a real abrupt process termination, not a
            # delivered signal, so it isn't subject to the PID-1 self-signal immunity.
            os._exit(137)

    background_tasks.add_task(task)
    return {"injected": "process_crash", "note": "container will be killed and auto-restarted"}


@app.post("/stop")
async def stop_app():
    _stop_event.set()
    return {"stopped": True}
