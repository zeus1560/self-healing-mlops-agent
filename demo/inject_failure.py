"""
데모용 장애 주입기 (Failure Injector)
=====================================
실시간 로그 파일에 장애 시나리오를 작성해 에이전트의 탐지 → 추론 → 조치 루프를 시연합니다.

사용법:
    # 인터랙티브 메뉴
    python demo/inject_failure.py

    # 단일 장애 주입
    python demo/inject_failure.py --type oom
    python demo/inject_failure.py --type disk_full

    # 전체 데모 시나리오 (경진대회 발표용)
    python demo/inject_failure.py --scenario full

    # 특정 로그 파일 대상
    python demo/inject_failure.py --type db_timeout --log-file data/realtime_system.log
"""

import argparse
import os
import random
import sys
import time
from datetime import datetime

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.columns import Columns
from rich import print as rprint

console = Console()

DEFAULT_LOG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "realtime_system.log"
)

# ── 장애 시나리오 정의 ─────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


SCENARIOS: dict[str, dict] = {
    "oom": {
        "name": "Out-of-Memory (OOM)",
        "category": "Out_Of_Memory",
        "color": "bold red",
        "icon": "💥",
        "description": "메모리 부족으로 OOM Killer가 프로세스를 종료",
        "lines": lambda: [
            f"{_ts()} INFO  kernel: [1234567.890] memory pressure detected on node 0",
            f"{_ts()} WARN  kernel: low memory threshold crossed (available: 42MB / 8192MB)",
            f"{_ts()} ERROR kernel: Out of memory: Kill process {random.randint(1000,9999)} (api-server) score {random.randint(800,999)} or sacrifice child",
            f"{_ts()} CRITICAL kernel: OOM killer invoked for process api-server (pid={random.randint(1000,9999)})",
            f"{_ts()} ERROR  api-server[{random.randint(1000,9999)}]: FATAL — process killed by OOM killer, memory usage was 7.8GB",
            f"{_ts()} ERROR  systemd[1]: api-server.service: Main process exited, code=killed, status=9/KILL",
        ],
    },
    "memory_leak": {
        "name": "Memory Leak",
        "category": "Memory_Leak",
        "color": "bold yellow",
        "icon": "🔴",
        "description": "워커 프로세스의 메모리 누수 지속 증가",
        "lines": lambda: [
            f"{_ts()} INFO  worker[{random.randint(100,999)}]: heap usage 512MB (normal)",
            f"{_ts()} WARN  worker[{random.randint(100,999)}]: heap usage 2.1GB — above warning threshold",
            f"{_ts()} ERROR worker[{random.randint(100,999)}]: CRITICAL memory leak detected in worker process {random.randint(8000,9000)}",
            f"{_ts()} ERROR worker[{random.randint(100,999)}]: RSS memory grew from 512MB to 6.2GB over 30 minutes — suspected memory leak",
            f"{_ts()} CRITICAL monitor: memory leak rate +120MB/min — intervention required",
        ],
    },
    "disk_full": {
        "name": "Disk Full",
        "category": "Disk_Full",
        "color": "bold magenta",
        "icon": "💾",
        "description": "디스크 용량 초과로 서비스 쓰기 실패",
        "lines": lambda: [
            f"{_ts()} WARN  df: /var/log filesystem usage at 89% (threshold: 85%)",
            f"{_ts()} WARN  logrotate: /dev/sda1 approaching capacity — 4.2GB remaining",
            f"{_ts()} ERROR postgres[{random.randint(1000,9999)}]: could not write to file 'pg_wal/000000010000012B': No space left on device",
            f"{_ts()} ERROR nginx[{random.randint(1000,9999)}]: open() '/var/log/nginx/access.log' failed (28: No space left on device)",
            f"{_ts()} CRITICAL systemd[1]: disk full — /dev/sda1 at 100% capacity, write operations failing",
            f"{_ts()} ERROR  journal: /var/log/journal: no space left — disk full on /dev/sda1",
        ],
    },
    "process_crash": {
        "name": "Process Crash",
        "category": "Process_Crash",
        "color": "bold red",
        "icon": "💀",
        "description": "핵심 서비스 프로세스 비정상 종료",
        "lines": lambda: [
            f"{_ts()} INFO  nginx[{random.randint(1000,9999)}]: worker process started (pid={random.randint(1000,9999)})",
            f"{_ts()} WARN  nginx[{random.randint(1000,9999)}]: upstream response timeout (30s) — 3rd consecutive failure",
            f"{_ts()} ERROR nginx[{random.randint(1000,9999)}]: worker process {random.randint(1000,9999)} exited with signal 11 (SIGSEGV)",
            f"{_ts()} CRITICAL nginx[1]: all worker processes crashed — service unavailable",
            f"{_ts()} ERROR  systemd[1]: nginx.service: Control process exited, code=dumped, status=11/SEGV",
            f"{_ts()} ERROR  systemd[1]: nginx.service: Failed with result 'core-dump'",
        ],
    },
    "port_conflict": {
        "name": "Port Conflict",
        "category": "Port_Conflict",
        "color": "cyan",
        "icon": "⚡",
        "description": "포트 충돌로 서비스 바인딩 실패",
        "lines": lambda: [
            f"{_ts()} INFO  api-server: attempting to bind on 0.0.0.0:{random.choice([8080,3000,5000,9090])}",
            f"{_ts()} WARN  api-server: port {random.choice([8080,3000,5000,9090])} health check failed — retrying",
            f"{_ts()} ERROR api-server[{random.randint(1000,9999)}]: bind: Address already in use — port {random.choice([8080,3000,5000,9090])} conflict detected",
            f"{_ts()} ERROR api-server[{random.randint(1000,9999)}]: Failed to start server: listen tcp 0.0.0.0:{random.choice([8080,3000,5000,9090])}: bind: address already in use",
            f"{_ts()} CRITICAL systemd[1]: api-server.service: Start request repeated too quickly",
        ],
    },
    "auth_error": {
        "name": "Auth Error",
        "category": "Auth_Error",
        "color": "bold yellow",
        "icon": "🔐",
        "description": "인증 실패 반복 — 토큰 만료 또는 자격증명 오류",
        "lines": lambda: [
            f"{_ts()} INFO  auth-service: token validation request from 10.0.{random.randint(1,255)}.{random.randint(1,255)}",
            f"{_ts()} WARN  auth-service: JWT token expired — issued_at=2026-05-13T09:00:00Z",
            f"{_ts()} ERROR auth-service[{random.randint(1000,9999)}]: Authentication failed — invalid or expired credentials (attempt 3/3)",
            f"{_ts()} ERROR auth-service[{random.randint(1000,9999)}]: AuthException: token signature verification failed for user_id={random.randint(10000,99999)}",
            f"{_ts()} CRITICAL auth-service: repeated auth failures — possible credential compromise or token rotation failure",
        ],
    },
    "db_timeout": {
        "name": "DB Connection Timeout",
        "category": "DB_Connection",
        "color": "bold blue",
        "icon": "🗄️",
        "description": "데이터베이스 연결 풀 고갈 및 타임아웃",
        "lines": lambda: [
            f"{_ts()} INFO  postgres-pool: active_connections=95/100",
            f"{_ts()} WARN  postgres-pool: connection pool at 98% capacity — queuing requests",
            f"{_ts()} ERROR postgres[{random.randint(1000,9999)}]: FATAL — connection Timeout after 30000ms: remaining connection slots reserved",
            f"{_ts()} ERROR api-server[{random.randint(1000,9999)}]: Database connection pool exhausted — all 100 connections in use",
            f"{_ts()} CRITICAL postgres: PostgreSQL Connection Timeout — unable to acquire connection within 30s",
            f"{_ts()} ERROR  api-server: db query failed: context deadline exceeded (timeout=30s)",
        ],
    },
    "network_timeout": {
        "name": "Network Timeout",
        "category": "Network_Timeout",
        "color": "blue",
        "icon": "🌐",
        "description": "외부 서비스 네트워크 연결 타임아웃",
        "lines": lambda: [
            f"{_ts()} INFO  http-client: GET https://api.internal/health → 200 OK (12ms)",
            f"{_ts()} WARN  http-client: upstream latency spike — p99=4200ms (SLO: 1000ms)",
            f"{_ts()} ERROR http-client[{random.randint(1000,9999)}]: Network Timeout — connection to 10.0.{random.randint(1,10)}.{random.randint(1,50)}:443 timed out after 5000ms",
            f"{_ts()} ERROR http-client[{random.randint(1000,9999)}]: Retries exhausted (3/3) — upstream service unreachable",
            f"{_ts()} CRITICAL load-balancer: backend 10.0.{random.randint(1,10)}.{random.randint(1,50)} marked unhealthy — network timeout threshold exceeded",
        ],
    },
    "permission_denied": {
        "name": "Permission Denied",
        "category": "Permission_Denied",
        "color": "yellow",
        "icon": "🚫",
        "description": "파일 시스템 권한 거부 — 설정 파일 접근 불가",
        "lines": lambda: [
            f"{_ts()} INFO  config-manager: reloading configuration from /etc/app/config.yaml",
            f"{_ts()} WARN  config-manager: /etc/app/config.yaml modified externally — reload triggered",
            f"{_ts()} ERROR config-manager[{random.randint(1000,9999)}]: PermissionError — [Errno 13] Permission denied: '/etc/nginx/nginx.conf'",
            f"{_ts()} ERROR config-manager[{random.randint(1000,9999)}]: cannot write to /var/run/app.pid: Permission denied",
            f"{_ts()} CRITICAL systemd[1]: CRITICAL PermissionError — agent cannot write to /etc/nginx/nginx.conf, manual intervention required",
        ],
    },
    "config_error": {
        "name": "Configuration Error",
        "category": "Configuration_Error",
        "color": "bright_cyan",
        "icon": "⚙️",
        "description": "잘못된 설정 파일로 서비스 시작 실패",
        "lines": lambda: [
            f"{_ts()} INFO  nginx: testing configuration /etc/nginx/nginx.conf",
            f"{_ts()} WARN  nginx: deprecated directive 'resolver_timeout' in /etc/nginx/sites-enabled/app",
            f"{_ts()} ERROR nginx[{random.randint(1000,9999)}]: Configuration Error — [emerg] unknown directive 'proxy_cache_methods' in /etc/nginx/nginx.conf:42",
            f"{_ts()} ERROR nginx[{random.randint(1000,9999)}]: configuration file /etc/nginx/nginx.conf test failed",
            f"{_ts()} CRITICAL systemd[1]: nginx.service: control process returned error code — configuration reload failed",
        ],
    },
}

# 전체 데모 시나리오 순서 (5개 — 발표 7~10분 기준)
FULL_SCENARIO_SEQUENCE = [
    "oom",
    "db_timeout",
    "disk_full",
    "process_crash",
    "auth_error",
]


# ── 핵심 주입 함수 ─────────────────────────────────────────────────────────────

def inject(scenario_key: str, log_file: str, verbose: bool = True) -> None:
    scenario = SCENARIOS[scenario_key]
    lines = scenario["lines"]()

    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)

    if verbose:
        console.print()
        console.print(Panel(
            f"[{scenario['color']}]{scenario['icon']}  {scenario['name']}[/]\n"
            f"[dim]카테고리: {scenario['category']} | 대상: {os.path.basename(log_file)}[/]\n"
            f"[white]{scenario['description']}[/]",
            title="[bold white]장애 주입[/]",
            border_style="red",
            padding=(0, 2),
        ))
        console.print(f"  [dim]주입 중...[/]")

    with open(log_file, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
            f.flush()
            if verbose:
                color = "red" if "CRITICAL" in line or "ERROR" in line else "dim"
                console.print(f"    [{color}]→ {line[:110]}[/]")
            time.sleep(0.15)

    if verbose:
        console.print(f"\n  [bold green]✓ {len(lines)}줄 주입 완료 — 에이전트 감지 대기 중...[/]\n")


# ── 대화형 메뉴 ───────────────────────────────────────────────────────────────

def show_menu() -> str:
    table = Table(
        title="[bold white]Self-Healing MLOps Agent — 데모 장애 주입기[/]",
        box=box.ROUNDED,
        border_style="blue",
        show_header=True,
        header_style="bold cyan",
        padding=(0, 1),
    )
    table.add_column("#", style="bold white", width=4)
    table.add_column("키", style="bold yellow", width=16)
    table.add_column("장애 유형", style="white", width=28)
    table.add_column("카테고리", style="dim", width=22)
    table.add_column("설명", style="dim")

    ordered = list(SCENARIOS.items())
    for idx, (key, s) in enumerate(ordered, 1):
        table.add_row(
            str(idx),
            key,
            f"[{s['color']}]{s['icon']} {s['name']}[/]",
            s["category"],
            s["description"],
        )

    table.add_section()
    table.add_row(
        "F",
        "full",
        "[bold green]★ 전체 데모 시나리오[/]",
        "5개 순서 자동 실행",
        "경진대회 발표용 — OOM → DB → Disk → Crash → Auth",
    )
    table.add_row("Q", "quit", "[dim]종료[/]", "", "")

    console.print()
    console.print(table)
    console.print()

    return ordered


def run_interactive(log_file: str) -> None:
    ordered = show_menu()
    keys = [k for k, _ in ordered]

    while True:
        try:
            choice = console.input("[bold white]선택 (번호/키/F/Q): [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]종료합니다.[/]")
            break

        if choice in ("q", "quit", ""):
            console.print("[dim]종료합니다.[/]")
            break

        if choice in ("f", "full"):
            run_full_scenario(log_file)
            break

        # 번호 입력
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                choice = keys[idx]
            else:
                console.print(f"[red]범위 초과: 1~{len(keys)} 사이를 입력하세요.[/]")
                continue

        if choice in SCENARIOS:
            inject(choice, log_file)
            try:
                again = console.input("[dim]계속 주입하시겠습니까? (y/n): [/]").strip().lower()
                if again != "y":
                    break
                ordered = show_menu()
                keys = [k for k, _ in ordered]
            except (EOFError, KeyboardInterrupt):
                break
        else:
            console.print(f"[red]알 수 없는 키: '{choice}'[/]")


# ── 전체 데모 시나리오 ────────────────────────────────────────────────────────

def run_full_scenario(log_file: str, delay: float = 18.0) -> None:
    console.print()
    console.print(Panel(
        "[bold white]전체 데모 시나리오 시작[/]\n"
        "[dim]에이전트를 먼저 실행해두세요 (AUTO_APPROVE 필수):[/]\n"
        f"[cyan]  AUTO_APPROVE=true .venv/bin/python -m src.log_watcher {log_file}[/]\n\n"
        f"[dim]장애 유형: {len(FULL_SCENARIO_SEQUENCE)}개 | 간격: {delay:.0f}초[/]",
        title="[bold green]★ 경진대회 데모[/]",
        border_style="green",
        padding=(0, 2),
    ))

    try:
        console.input("[dim]준비되면 Enter를 누르세요...[/]")
    except (EOFError, KeyboardInterrupt):
        return

    total = len(FULL_SCENARIO_SEQUENCE)
    for step, key in enumerate(FULL_SCENARIO_SEQUENCE, 1):
        s = SCENARIOS[key]
        console.rule(
            f"[bold white] STEP {step}/{total} — {s['icon']} {s['name']} [/]",
            style="yellow",
        )
        inject(key, log_file, verbose=True)

        if step < total:
            _countdown(delay, f"다음 장애까지 대기 ({SCENARIOS[FULL_SCENARIO_SEQUENCE[step]]['name']})")

    console.print()
    console.print(Panel(
        "[bold green]✓ 전체 데모 시나리오 완료[/]\n"
        "[dim]에이전트 메트릭 대시보드에서 결과를 확인하세요:[/]\n"
        "[cyan]  streamlit run dashboard/app.py[/]",
        border_style="green",
        padding=(0, 2),
    ))


def _countdown(seconds: float, label: str = "") -> None:
    end = time.time() + seconds
    try:
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            msg = f"  [dim]{label} — [bold white]{remaining:.0f}s[/][/]"
            console.print(msg, end="\r")
            time.sleep(0.5)
        console.print(" " * 80, end="\r")  # clear line
    except KeyboardInterrupt:
        console.print("\n[yellow]대기 건너뜀[/]")


# ── CLI 진입점 ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Self-Healing MLOps Agent — 데모 장애 주입기",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
장애 유형 목록:
  oom              Out-of-Memory (OOM Killer)
  memory_leak      Memory Leak
  disk_full        Disk Full
  process_crash    Process Crash (SIGSEGV)
  port_conflict    Port Conflict
  auth_error       Authentication Error
  db_timeout       DB Connection Timeout
  network_timeout  Network Timeout
  permission_denied Permission Denied
  config_error     Configuration Error

예시:
  python demo/inject_failure.py
  python demo/inject_failure.py --type oom
  python demo/inject_failure.py --scenario full --delay 20
        """,
    )
    parser.add_argument(
        "--type", "-t",
        choices=list(SCENARIOS.keys()),
        metavar="TYPE",
        help="주입할 장애 유형 (생략 시 인터랙티브 메뉴)",
    )
    parser.add_argument(
        "--scenario", "-s",
        choices=["full"],
        help="전체 데모 시나리오 실행 (full)",
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=18.0,
        help="시나리오 모드에서 장애 간 대기 시간 (초, 기본: 18)",
    )
    parser.add_argument(
        "--log-file", "-l",
        default=DEFAULT_LOG_FILE,
        help=f"대상 로그 파일 경로 (기본: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="장애 유형 목록 출력 후 종료",
    )

    args = parser.parse_args()

    if args.list:
        for key, s in SCENARIOS.items():
            console.print(f"  [bold yellow]{key:<20}[/] {s['icon']} {s['name']} — {s['description']}")
        sys.exit(0)

    log_file = os.path.abspath(args.log_file)

    console.print(
        f"\n[dim]대상 로그: [cyan]{log_file}[/][/]"
        f"\n[dim]에이전트가 실행 중이어야 정상 작동합니다.[/]\n"
    )

    if args.scenario == "full":
        run_full_scenario(log_file, delay=args.delay)
    elif args.type:
        inject(args.type, log_file)
    else:
        run_interactive(log_file)


if __name__ == "__main__":
    main()
