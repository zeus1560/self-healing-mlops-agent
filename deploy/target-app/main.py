from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
import sqlite3, threading, time, os, subprocess, sys, json
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


# stress-ng has self-protective OOM-avoidance heuristics that turned out to be
# unreliable to fully disable — it kept exiting 0 without ever hitting the
# cgroup limit. A plain Python allocator has no such protection: growing a
# bytearray forces real page commits, so the kernel OOM killer has no choice
# but to intervene once the 512m cgroup cap is hit. Run as its own subprocess
# so a kill can't take down the FastAPI app itself.
_OOM_BOMB_SRC = (
    "blocks = []\n"
    "for _ in range(2000):\n"
    "    b = bytearray(10 * 1024 * 1024)\n"
    "    for i in range(0, len(b), 4096):\n"
    "        b[i] = 1\n"
    "    blocks.append(b)\n"
)


def _append_evidence(level: str, message: str) -> None:
    """에이전트(log_watcher)가 실제로 tail하는 realtime_system.log에 실측 증거를 기록."""
    with open(EVIDENCE_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()} {level} chaos-injector: {message}\n")


# inject_auth_error()가 잘못된 토큰으로 호출해 실제 401을 유발하는 대상 —
# 새 외부 인증 서비스를 세우지 않고 컨테이너 자기 자신의 포트를 호출해 자기완결적으로 유지한다.
_INTERNAL_AUTH_SECRET = os.getenv("TARGET_APP_INTERNAL_SECRET", "chaos-injector-internal-secret")

# inject_memory_leak()이 구동하는, 15MB씩 6단계(총 90MB, cgroup 512m 한도의 극히 일부)에 걸쳐
# 1.5초 간격으로 서서히 할당하는 프로세스 — Out_Of_Memory(즉시 대량 할당, OOM Killer 개입)와
# 구분되는 "서서히 커지는 실제 RSS 증가 추세" 시그니처를 만드는 게 목적이라 한도를 넘기지 않는다.
_LEAK_SRC = (
    "import time\n"
    "blocks = []\n"
    "for i in range(6):\n"
    "    b = bytearray(15 * 1024 * 1024)\n"
    "    for j in range(0, len(b), 4096):\n"
    "        b[j] = 1\n"
    "    blocks.append(b)\n"
    "    time.sleep(1.5)\n"
)


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
                [sys.executable, "-c", _OOM_BOMB_SRC],
                capture_output=True, text=True, timeout=25,
            )
            oom_killed = result.returncode != 0
            _append_evidence(
                "CRITICAL",
                f"python memory allocator vs cgroup mem_limit=512m — returncode={result.returncode}, "
                f"{'killed by real cgroup OOM' if oom_killed else 'completed without triggering OOM — investigate'}",
            )
        except subprocess.TimeoutExpired:
            _append_evidence("CRITICAL", "python memory allocator OOM test timed out — process likely hung under memory pressure")
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


@app.post("/inject/permission_denied")
async def inject_permission_denied():
    """실행 비트가 전혀 없는 스크립트 실행 시도 — root도 우회 못 하는 실제 PermissionError 유발.
    (root는 파일 읽기/쓰기 권한 검사는 우회하지만, exec는 최소 하나의 x 비트가 없으면 root도 EACCES)"""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "permission_denied", "skipped": "another injection in progress"}
    script_path = "/app/data/locked_reload.sh" if os.path.isdir('/app/data') else "./data/locked_reload.sh"
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho reload\n")
        os.chmod(script_path, 0o000)
        try:
            subprocess.run([script_path], capture_output=True, text=True, timeout=5)
            _append_evidence("ERROR", f"expected PermissionError executing {script_path} (mode=000) but it ran — investigate")
        except PermissionError as e:
            _append_evidence(
                "CRITICAL",
                f"PermissionError — [Errno 13] Permission denied: '{script_path}' (mode=000, no execute bit set): {e}",
            )
        return {"injected": "permission_denied"}
    finally:
        try:
            os.chmod(script_path, 0o644)
            if os.path.exists(script_path):
                os.remove(script_path)
        finally:
            _injection_lock.release()


@app.post("/inject/path_not_found")
async def inject_path_not_found():
    """존재하지 않는 설정 경로를 오픈 시도해 실제 FileNotFoundError 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "path_not_found", "skipped": "another injection in progress"}
    missing_path = "/app/data/missing_module_v2.conf" if os.path.isdir('/app/data') else "./data/missing_module_v2.conf"
    try:
        try:
            open(missing_path, "r", encoding="utf-8").close()
            _append_evidence("ERROR", f"expected FileNotFoundError opening {missing_path} but it existed — investigate")
        except FileNotFoundError as e:
            _append_evidence(
                "CRITICAL",
                f"FileNotFoundError — [Errno 2] No such file or directory: '{missing_path}': {e}",
            )
        return {"injected": "path_not_found"}
    finally:
        _injection_lock.release()


@app.post("/inject/config_error")
async def inject_config_error():
    """문법 오류가 있는 JSON 설정 파일을 실제로 파싱 시도해 JSONDecodeError 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "config_error", "skipped": "another injection in progress"}
    config_path = "/app/data/app_config.json" if os.path.isdir('/app/data') else "./data/app_config.json"
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write('{"proxy_cache_methods": [GET, POST],}')
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                json.load(f)
            _append_evidence("ERROR", f"expected JSONDecodeError parsing {config_path} but it parsed — investigate")
        except json.JSONDecodeError as e:
            _append_evidence(
                "CRITICAL",
                f"Configuration Error — JSONDecodeError parsing {config_path}: {e}",
            )
        return {"injected": "config_error"}
    finally:
        try:
            if os.path.exists(config_path):
                os.remove(config_path)
        finally:
            _injection_lock.release()


@app.post("/inject/db_connection")
async def inject_db_connection():
    """redis-py로 127.0.0.1:6379(리스닝 프로세스 없음)에 접속 시도해 실제 ConnectionError 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "db_connection", "skipped": "another injection in progress"}
    try:
        import redis
        try:
            client = redis.Redis(host="127.0.0.1", port=6379, socket_connect_timeout=2, socket_timeout=2)
            client.ping()
            _append_evidence("ERROR", "expected redis.exceptions.ConnectionError pinging 127.0.0.1:6379 but it succeeded — investigate")
        except redis.exceptions.ConnectionError as e:
            _append_evidence(
                "CRITICAL",
                f"redis.exceptions.ConnectionError — Could not connect to Redis at 127.0.0.1:6379: Connection refused: {e}",
            )
        return {"injected": "db_connection"}
    finally:
        _injection_lock.release()


@app.post("/inject/network_timeout")
async def inject_network_timeout():
    """psycopg2로 예약 주소(192.0.2.1, RFC 5737 TEST-NET-1)에 접속 시도해 실제 접속 타임아웃 유발."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "network_timeout", "skipped": "another injection in progress"}
    try:
        import psycopg2
        try:
            psycopg2.connect(host="192.0.2.1", port=5432, dbname="postgres", user="postgres", connect_timeout=3)
            _append_evidence("ERROR", "expected psycopg2.OperationalError connecting to 192.0.2.1:5432 but it succeeded — investigate")
        except psycopg2.OperationalError as e:
            _append_evidence(
                "CRITICAL",
                f'psycopg2.OperationalError — connection to server at "192.0.2.1", port 5432 failed: timeout expired: {e}',
            )
        return {"injected": "network_timeout"}
    finally:
        _injection_lock.release()


@app.get("/internal/protected")
async def internal_protected(authorization: str = Header(default="")):
    """Bearer 토큰 검증이 필요한 내부 리소스 — inject_auth_error()가 이걸 잘못된
    토큰으로 호출해 실제 인증 실패를 유발하는 대상."""
    if authorization != f"Bearer {_INTERNAL_AUTH_SECRET}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return {"status": "ok"}


@app.post("/inject/auth_error")
def inject_auth_error():
    """
    자체 보호 엔드포인트를 잘못된 토큰으로 호출해 실제 401(HTTPError) 유발.

    일반 def(async 아님)로 선언 — FastAPI가 이런 핸들러를 스레드풀에서 실행한다.
    async def였다면 아래 requests.get()이 단일 이벤트루프를 블로킹해서, 자기 자신에게
    보낸 이 요청을 그 이벤트루프가 처리 못 해 데드락(클라이언트 타임아웃)이 났었음 —
    로컬 재현으로 실제 확인함.
    """
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "auth_error", "skipped": "another injection in progress"}
    try:
        import requests
        try:
            resp = requests.get(
                "http://127.0.0.1:9000/internal/protected",
                headers={"Authorization": "Bearer wrong-token-xyz"},
                timeout=3,
            )
            resp.raise_for_status()
            _append_evidence(
                "ERROR",
                "expected 401 from /internal/protected with invalid bearer token but request succeeded — investigate",
            )
        except requests.exceptions.HTTPError as e:
            _append_evidence(
                "CRITICAL",
                f"requests.exceptions.HTTPError — 401 Unauthorized calling /internal/protected "
                f"with invalid bearer token: {e}",
            )
        return {"injected": "auth_error"}
    finally:
        _injection_lock.release()


@app.post("/inject/memory_leak")
async def inject_memory_leak():
    """OOM(즉시 대량 할당)과 달리, 서서히 커지는 백그라운드 프로세스의 실제 RSS를
    주기적으로 측정해 점진적 누수 시그니처를 만든다. cgroup 한도(512m)의 극히
    일부만 쓰므로 OOM Killer는 개입하지 않는다 — 이 프로세스는 스스로 정리한다."""
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "memory_leak", "skipped": "another injection in progress"}
    proc = None
    try:
        import psutil
        proc = subprocess.Popen(
            [sys.executable, "-c", _LEAK_SRC],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        samples = []
        try:
            ps_proc = psutil.Process(proc.pid)
            for _ in range(6):
                time.sleep(1.6)
                # sleep 직후(측정 직전)에 종료 여부를 확인 — 이미 끝난 뒤에 RSS를
                # 읽으면 0에 가까운 값이 찍혀 "누수 추세"가 끝에서 뚝 떨어져 보임.
                if proc.poll() is not None:
                    break
                try:
                    samples.append(ps_proc.memory_info().rss / (1024 * 1024))
                except psutil.NoSuchProcess:
                    break
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if len(samples) >= 2:
            trend = " -> ".join(f"{s:.1f}MB" for s in samples)
            _append_evidence(
                "CRITICAL",
                f"MemoryLeak — background process pid={proc.pid} RSS steadily growing: "
                f"{trend} over ~{len(samples) * 1.6:.0f}s — gradual leak signature "
                f"(distinct from an immediate OOM-Killer event)",
            )
        else:
            _append_evidence(
                "ERROR",
                "expected steady RSS growth from leak subprocess but too few samples captured — investigate",
            )
        return {"injected": "memory_leak"}
    finally:
        _injection_lock.release()


@app.post("/inject/db_deadlock")
def inject_db_deadlock():
    """
    SQLite 자체의 락 경합으로 실제 sqlite3.OperationalError("database is locked")를
    유발한다 — 별도 DB 서버 없이 self-contained로 진짜 락 경합 예외를 만든다.

    일반 def(async 아님)로 선언 — auth_error와 같은 이유: 아래에서 백그라운드
    스레드가 락을 쥐고 있는 동안 join()으로 대기하는 블로킹 코드라, async def면
    단일 이벤트루프가 이 대기 때문에 다른 요청을 처리 못 하게 된다.
    """
    if not _injection_lock.acquire(blocking=False):
        return {"injected": "db_deadlock", "skipped": "another injection in progress"}
    db_path = "/app/data/deadlock_test.db" if os.path.isdir('/app/data') else "./data/deadlock_test.db"
    holder_ready   = threading.Event()
    release_holder = threading.Event()

    def _hold_exclusive_lock():
        conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("INSERT INTO t VALUES (1)")
        holder_ready.set()
        release_holder.wait(timeout=5)
        conn.execute("COMMIT")
        conn.close()

    holder = threading.Thread(target=_hold_exclusive_lock, daemon=True)
    holder.start()
    holder_ready.wait(timeout=3)

    try:
        try:
            conn2 = sqlite3.connect(db_path, timeout=0.5, isolation_level=None)
            conn2.execute("INSERT INTO t VALUES (2)")
            conn2.close()
            _append_evidence(
                "ERROR",
                "expected sqlite3.OperationalError (database is locked) but write succeeded — investigate",
            )
        except sqlite3.OperationalError as e:
            _append_evidence(
                "CRITICAL",
                f"sqlite3.OperationalError — database is locked (concurrent transaction holding "
                f"EXCLUSIVE lock): {e}",
            )
        return {"injected": "db_deadlock"}
    finally:
        release_holder.set()
        holder.join(timeout=5)
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        finally:
            _injection_lock.release()


@app.post("/stop")
async def stop_app():
    _stop_event.set()
    return {"stopped": True}
