"""
데모용 Vector DB 세팅 스크립트
================================
inject_failure.py 가 주입하는 syslog 형식 로그와 정확히 매칭되는
훈련 데이터를 ChromaDB에 추가합니다.

기존 GitHub Issues 기반 훈련 데이터는 포맷이 달라 실제 syslog 로그와
의미적으로 멀기 때문에 L1 캐시가 엉뚱한 액션을 선택하는 문제를 해결합니다.

실행:
    python demo/setup_demo_db.py
    python demo/setup_demo_db.py --verify   # 추가 후 검색 결과 확인
    python demo/setup_demo_db.py --remove   # 추가했던 데모 항목만 삭제
"""

import argparse
import hashlib
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import chromadb
from chromadb.config import Settings
from rich import print as rprint
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

CHROMA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "chroma_db")
COLLECTION_NAME = "error_playbook_vectors"
DEMO_ID_PREFIX = "demo_v1_"


# ── 데모 훈련 데이터 ───────────────────────────────────────────────────────────
# 각 시나리오당 5개 항목 → 5/5 앙상블 투표로 항상 같은 액션 선택
# command 필드: EXECUTE_RULE_COMMAND 시 실제 실행될 명령어 (화이트리스트 통과 확인 完)
# 사용 가능한 서비스: rsyslog, cron (systemctl로 실제 재시작 가능)
# ─────────────────────────────────────────────────────────────────────────────

DEMO_ENTRIES = [

    # ── 1. Out-of-Memory ─────────────────────────────────────────────────────
    {
        "log_text": "ERROR kernel: Out of memory: Kill process api-server score OOM sacrifice child",
        "error_category": "Out_Of_Memory",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "OOM killer 발동 — 메모리 최적화 및 캐시 해제로 안정화",
    },
    {
        "log_text": "CRITICAL kernel: OOM killer invoked for process api-server memory pressure",
        "error_category": "Out_Of_Memory",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "커널 OOM killer 작동 — 가비지 컬렉션 + GPU 캐시 초기화",
    },
    {
        "log_text": "ERROR api-server FATAL process killed by OOM killer memory usage 7.8GB",
        "error_category": "Out_Of_Memory",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "프로세스가 OOM으로 종료됨 — 메모리 즉시 회수",
    },
    {
        "log_text": "ERROR systemd api-server.service Main process exited code=killed status=9/KILL",
        "error_category": "Out_Of_Memory",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "서비스 강제 종료 (OOM) — 메모리 최적화로 재발 방지",
    },
    {
        "log_text": "WARN kernel low memory threshold crossed available 42MB OOM imminent",
        "error_category": "Out_Of_Memory",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "메모리 임계치 초과 — 선제적 메모리 회수 실행",
    },

    # ── 2. Memory Leak ───────────────────────────────────────────────────────
    {
        "log_text": "ERROR worker CRITICAL memory leak detected in worker process heap RSS growing",
        "error_category": "Memory_Leak",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "메모리 누수 감지 — gc 강제 수행으로 누수 회수",
    },
    {
        "log_text": "CRITICAL monitor memory leak rate per minute intervention required heap unbounded",
        "error_category": "Memory_Leak",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "메모리 누수 속도 위험 수준 — 즉시 메모리 정리",
    },
    {
        "log_text": "ERROR worker heap usage 6.2GB suspected memory leak RSS grew from 512MB",
        "error_category": "Memory_Leak",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "힙 메모리 비정상 증가 — 가비지 컬렉션 실행",
    },
    {
        "log_text": "WARN worker heap usage above warning threshold memory leak suspected",
        "error_category": "Memory_Leak",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "워커 메모리 경고 임계치 초과 — 예방적 메모리 회수",
    },
    {
        "log_text": "ERROR process memory leak RSS 5GB unbounded growth worker crash imminent",
        "error_category": "Memory_Leak",
        "action_type": "clear_memory",
        "target_process": None,
        "command": None,
        "reasoning": "메모리 누수로 인한 프로세스 위험 — 즉시 조치",
    },

    # ── 3. Disk Full ─────────────────────────────────────────────────────────
    {
        "log_text": "ERROR postgres could not write to file pg_wal No space left on device disk full",
        "error_category": "Disk_Full",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "디스크 풀 — 저널 로그 정리로 공간 확보",
    },
    {
        "log_text": "CRITICAL systemd disk full /dev/sda1 at 100% capacity write operations failing",
        "error_category": "Disk_Full",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "디스크 용량 초과 — 오래된 저널 항목 삭제",
    },
    {
        "log_text": "ERROR nginx open /var/log/nginx/access.log failed 28 No space left on device",
        "error_category": "Disk_Full",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "Nginx 로그 쓰기 실패 (디스크 풀) — 저널 정리",
    },
    {
        "log_text": "ERROR journal /var/log/journal no space left disk full /dev/sda1 write failed",
        "error_category": "Disk_Full",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "저널 디스크 풀 — vacuum으로 아카이브 정리",
    },
    {
        "log_text": "WARN df /var/log filesystem usage 89% approaching capacity disk space critical",
        "error_category": "Disk_Full",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "디스크 사용량 경계치 접근 — 예방적 정리 실행",
    },

    # ── 4. Process Crash ─────────────────────────────────────────────────────
    {
        "log_text": "ERROR nginx worker process exited with signal 11 SIGSEGV segmentation fault",
        "error_category": "Process_Crash",
        "action_type": "restart_service",
        "target_process": "rsyslog",
        "command": None,
        "reasoning": "프로세스 비정상 종료 (SIGSEGV) — 로깅 서비스 재시작으로 안정화",
    },
    {
        "log_text": "CRITICAL nginx all worker processes crashed service unavailable core dump",
        "error_category": "Process_Crash",
        "action_type": "restart_service",
        "target_process": "rsyslog",
        "command": None,
        "reasoning": "모든 워커 크래시 — 시스템 로깅 재시작으로 진단 데이터 수집",
    },
    {
        "log_text": "ERROR systemd nginx.service Control process exited code=dumped status=11/SEGV",
        "error_category": "Process_Crash",
        "action_type": "restart_service",
        "target_process": "rsyslog",
        "command": None,
        "reasoning": "서비스 코어 덤프 — 로깅 서비스 재시작 후 진단",
    },
    {
        "log_text": "ERROR systemd nginx.service Failed with result core-dump process crash",
        "error_category": "Process_Crash",
        "action_type": "restart_service",
        "target_process": "rsyslog",
        "command": None,
        "reasoning": "서비스 실패 결과 core-dump — 재시작으로 복구 시도",
    },
    {
        "log_text": "WARN nginx upstream response timeout 30s consecutive failure worker dying",
        "error_category": "Process_Crash",
        "action_type": "restart_service",
        "target_process": "rsyslog",
        "command": None,
        "reasoning": "연속 타임아웃으로 워커 비정상 종료 예상 — 로깅 재시작",
    },

    # ── 5. Port Conflict ─────────────────────────────────────────────────────
    {
        "log_text": "ERROR api-server bind Address already in use port conflict detected 8080",
        "error_category": "Port_Conflict",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "포트 충돌 감지 — 포트 점유 프로세스 확인",
    },
    {
        "log_text": "ERROR api-server Failed to start server listen tcp 0.0.0.0:8080 bind address already in use",
        "error_category": "Port_Conflict",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "TCP 포트 바인딩 실패 — 현재 포트 사용 현황 진단",
    },
    {
        "log_text": "CRITICAL systemd api-server.service Start request repeated too quickly port bind failure",
        "error_category": "Port_Conflict",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "서비스 반복 실패 (포트 충돌) — 네트워크 소켓 상태 점검",
    },
    {
        "log_text": "ERROR server port already in use bind failed address in use EADDRINUSE",
        "error_category": "Port_Conflict",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "EADDRINUSE — 리스닝 포트 현황 확인 후 충돌 프로세스 식별",
    },
    {
        "log_text": "WARN api-server port health check failed retrying connection refused port conflict",
        "error_category": "Port_Conflict",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "포트 점검 실패 — 소켓 상태 스캔",
    },

    # ── 6. Auth Error ────────────────────────────────────────────────────────
    {
        "log_text": "ERROR auth-service Authentication failed invalid or expired credentials attempt 3/3",
        "error_category": "Auth_Error",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "인증 3회 연속 실패 — 자동 조치 위험, 보안 담당자 에스컬레이션",
    },
    {
        "log_text": "ERROR auth-service AuthException token signature verification failed user_id",
        "error_category": "Auth_Error",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "JWT 서명 검증 실패 — 토큰 로테이션 또는 침해 가능성, 인간 판단 필요",
    },
    {
        "log_text": "CRITICAL auth-service repeated auth failures credential compromise token rotation failure",
        "error_category": "Auth_Error",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "반복 인증 실패 — 자격증명 침해 가능성, 보안팀 즉시 에스컬레이션",
    },
    {
        "log_text": "WARN auth-service JWT token expired issued_at credential validation failed",
        "error_category": "Auth_Error",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "JWT 만료 — 토큰 갱신 정책 검토 필요",
    },
    {
        "log_text": "ERROR auth-service unauthorized 401 403 token invalid expired authentication failed",
        "error_category": "Auth_Error",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "인증 오류 — 보안 정책 위반 여부 확인 필요",
    },

    # ── 7. DB Connection Timeout ─────────────────────────────────────────────
    {
        "log_text": "ERROR postgres FATAL connection Timeout after 30000ms remaining connection slots reserved",
        "error_category": "DB_Connection",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "free -h",
        "reasoning": "DB 연결 타임아웃 — 시스템 메모리 상태 진단으로 원인 파악",
    },
    {
        "log_text": "CRITICAL postgres PostgreSQL Connection Timeout unable to acquire connection 30s",
        "error_category": "DB_Connection",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "free -h",
        "reasoning": "PostgreSQL 연결 불가 — 메모리 부족으로 인한 pool 고갈 진단",
    },
    {
        "log_text": "ERROR api-server Database connection pool exhausted all 100 connections in use",
        "error_category": "DB_Connection",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "free -h",
        "reasoning": "DB 커넥션 풀 고갈 — 시스템 리소스 현황 확인",
    },
    {
        "log_text": "ERROR api-server db query failed context deadline exceeded timeout 30s database",
        "error_category": "DB_Connection",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "free -h",
        "reasoning": "DB 쿼리 타임아웃 — 메모리/스왑 상태 확인",
    },
    {
        "log_text": "WARN postgres-pool connection pool 98% capacity queuing requests DB slow",
        "error_category": "DB_Connection",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "free -h",
        "reasoning": "연결 풀 포화 상태 — 리소스 모니터링",
    },

    # ── 8. Network Timeout ───────────────────────────────────────────────────
    {
        "log_text": "ERROR http-client Network Timeout connection timed out after 5000ms upstream unreachable",
        "error_category": "Network_Timeout",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "네트워크 타임아웃 — 리스닝 소켓 및 연결 상태 진단",
    },
    {
        "log_text": "CRITICAL load-balancer backend marked unhealthy network timeout threshold exceeded",
        "error_category": "Network_Timeout",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "백엔드 비정상 — 네트워크 소켓 상태 점검",
    },
    {
        "log_text": "ERROR http-client Retries exhausted 3/3 upstream service unreachable connection refused",
        "error_category": "Network_Timeout",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "재시도 소진 — 현재 열린 연결 상태 확인",
    },
    {
        "log_text": "WARN http-client upstream latency spike p99 4200ms SLO 1000ms timeout imminent",
        "error_category": "Network_Timeout",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "레이턴시 급증 — 네트워크 연결 상태 점검",
    },
    {
        "log_text": "ERROR network connection timed out socket timeout ETIMEDOUT unreachable host",
        "error_category": "Network_Timeout",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "ss -tuln",
        "reasoning": "ETIMEDOUT — 소켓 연결 현황 스캔",
    },

    # ── 9. Permission Denied ─────────────────────────────────────────────────
    {
        "log_text": "ERROR config-manager PermissionError Errno 13 Permission denied /etc/nginx/nginx.conf",
        "error_category": "Permission_Denied",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "권한 거부 — 파일 시스템 권한 변경은 자동화 불가, 에스컬레이션",
    },
    {
        "log_text": "CRITICAL systemd CRITICAL PermissionError agent cannot write manual intervention required",
        "error_category": "Permission_Denied",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "설정 파일 쓰기 권한 없음 — 수동 개입 필요",
    },
    {
        "log_text": "ERROR config-manager cannot write to /var/run/app.pid Permission denied EACCES",
        "error_category": "Permission_Denied",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "PID 파일 생성 권한 없음 — chmod/chown 필요, 에스컬레이션",
    },
    {
        "log_text": "ERROR permission denied access forbidden file system write blocked EPERM root required",
        "error_category": "Permission_Denied",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "파일 시스템 권한 오류 — 관리자 판단 필요",
    },
    {
        "log_text": "WARN config-manager /etc/app/config.yaml permission denied reload failed access error",
        "error_category": "Permission_Denied",
        "action_type": "escalate_to_human",
        "target_process": None,
        "command": None,
        "reasoning": "설정 파일 접근 권한 거부 — 에스컬레이션",
    },

    # ── 10. Configuration Error ──────────────────────────────────────────────
    {
        "log_text": "ERROR nginx Configuration Error emerg unknown directive proxy_cache_methods nginx.conf",
        "error_category": "Configuration_Error",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "Nginx 설정 오류 — 저널 정리 후 진단 데이터 확보",
    },
    {
        "log_text": "ERROR nginx configuration file /etc/nginx/nginx.conf test failed invalid config",
        "error_category": "Configuration_Error",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "Nginx 설정 검증 실패 — 로그 공간 확보 후 재시도",
    },
    {
        "log_text": "CRITICAL systemd nginx.service control process returned error code configuration reload failed",
        "error_category": "Configuration_Error",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "서비스 설정 오류 — 저널 공간 확보",
    },
    {
        "log_text": "ERROR config invalid configuration syntax parse error file line directive unknown",
        "error_category": "Configuration_Error",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "설정 파싱 실패 — 로그 공간 정리",
    },
    {
        "log_text": "WARN nginx deprecated directive configuration warning syntax error reload",
        "error_category": "Configuration_Error",
        "action_type": "execute_rule_command",
        "target_process": None,
        "command": "journalctl --vacuum-size 1G",
        "reasoning": "설정 지시문 경고 — 저널 정리 후 설정 검토",
    },
]


# ── ChromaDB 조작 함수 ────────────────────────────────────────────────────────

def _get_collection():
    client = chromadb.PersistentClient(
        path=os.path.abspath(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(name=COLLECTION_NAME)


def _make_id(log_text: str) -> str:
    return DEMO_ID_PREFIX + hashlib.md5(log_text.encode("utf-8")).hexdigest()


def add_demo_entries(collection) -> int:
    ids, docs, metas = [], [], []
    for entry in DEMO_ENTRIES:
        ids.append(_make_id(entry["log_text"]))
        docs.append(entry["log_text"])
        metas.append({
            "error_category": entry["error_category"],
            "action_type":    entry["action_type"],
            "target_process": entry.get("target_process") or "unknown",
            "command":        entry.get("command") or "",
            "reasoning":      entry.get("reasoning", ""),
        })

    collection.upsert(ids=ids, documents=docs, metadatas=metas)
    return len(ids)


def remove_demo_entries(collection) -> int:
    try:
        results = collection.get(where={"action_type": {"$in": [
            "clear_memory", "execute_rule_command", "restart_service", "escalate_to_human"
        ]}})
    except Exception:
        results = {"ids": []}

    demo_ids = [id_ for id_ in results.get("ids", []) if id_.startswith(DEMO_ID_PREFIX)]
    if demo_ids:
        collection.delete(ids=demo_ids)
    return len(demo_ids)


def verify_entries(collection) -> None:
    test_queries = [
        ("OOM",         "ERROR kernel: Out of memory: Kill process api-server OOM killer invoked"),
        ("Disk Full",   "CRITICAL systemd disk full /dev/sda1 at 100% capacity write operations failing"),
        ("Process",     "ERROR nginx worker process exited with signal 11 SIGSEGV all workers crashed"),
        ("DB Timeout",  "CRITICAL postgres PostgreSQL Connection Timeout unable to acquire connection"),
        ("Auth Error",  "CRITICAL auth-service repeated auth failures possible credential compromise"),
    ]

    table = Table(
        title="[bold white]데모 DB 검증 — Top-1 매칭 결과[/]",
        box=box.ROUNDED, border_style="cyan",
        show_header=True, header_style="bold cyan",
    )
    table.add_column("시나리오", style="white", width=14)
    table.add_column("매칭된 문서", style="dim", width=46)
    table.add_column("거리", style="bold", width=8)
    table.add_column("액션", style="bold green", width=22)
    table.add_column("커맨드", style="yellow")

    for label, query in test_queries:
        r = collection.query(query_texts=[query], n_results=1)
        if r["documents"][0]:
            doc   = r["documents"][0][0][:44] + "…"
            dist  = f"{r['distances'][0][0]:.4f}"
            meta  = r["metadatas"][0][0]
            action = meta.get("action_type", "?")
            cmd    = meta.get("command", "") or "(내장 조치)"
            color  = "green" if r["distances"][0][0] < 0.3 else "yellow"
            table.add_row(label, doc, f"[{color}]{dist}[/]", action, cmd)
        else:
            table.add_row(label, "[red]검색 결과 없음[/]", "-", "-", "-")

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]거리 < 0.3 → 정확한 매칭 / < 0.8 → 유사 매칭[/]")


# ── 진입점 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="데모용 Vector DB 세팅")
    parser.add_argument("--verify", action="store_true", help="추가 후 검색 검증만 실행")
    parser.add_argument("--remove", action="store_true", help="데모 항목 삭제")
    args = parser.parse_args()

    collection = _get_collection()
    before = collection.count()

    if args.remove:
        n = remove_demo_entries(collection)
        console.print(f"\n[bold red]✓ 데모 항목 {n}건 삭제 완료[/] (전체: {collection.count()}건)\n")
        return

    if args.verify:
        verify_entries(collection)
        return

    console.print(f"\n[dim]기존 Vector DB: {before}건[/]")
    n = add_demo_entries(collection)
    after = collection.count()
    console.print(f"[bold green]✓ 데모 훈련 데이터 {n}건 추가 완료[/] ({before} → {after}건)\n")

    verify_entries(collection)

    console.print(
        "[dim]이제 에이전트를 재시작하면 데모 시나리오에서 올바른 액션이 실행됩니다.[/]\n"
        "[cyan]  .venv/bin/python -m src.log_watcher data/realtime_system.log[/]\n"
    )


if __name__ == "__main__":
    main()
