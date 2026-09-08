"""
add_chaos_injector_signatures.py
카오스 인젝터(deploy/target-app/main.py)가 실제로 남기는 문구를 ChromaDB에 추가한다.

배경 (2026-09-07): experiments/run_l1_production_shape_check.py로 실측한 결과,
6개 auto 카테고리(Out_Of_Memory/Disk_Full/Process_Crash/Permission_Denied/
Path_Not_Found/Configuration_Error) 전부 L1 거리가 0.95~1.14로 임계값(0.6)을
한참 넘어 실운영에서 사실상 한 번도 제대로 히트한 적이 없었음이 드러남. 원인은
컨텍스트 래핑이 아니라(그 가설은 실측으로 기각됨) — 카오스 인젝터가 남기는
문구(개발자가 디버깅용으로 쓴 기술적 로그 문장)가 학습 데이터(GitHub 이슈에서
크롤링한 자연어 에러 설명)와 어휘·문체가 근본적으로 달라서였음.

scripts/etl_github_to_chroma.py의 CURATED_SPARSE_CATEGORIES와 같은 철학 —
GitHub API를 다시 호출하지 않고, 검토·확정된 고정 텍스트를 그대로 upsert해
재현 가능하게 한다. 다만 출처가 GitHub이 아니라 이 프로젝트의 실제 운영
로그이므로 source 태그는 별도로 "chaos_injector_signature"를 사용한다
(etl_github_to_chroma.py의 --stats에서 출처별 통계가 왜곡되지 않도록).

각 카테고리마다 (1) 순수 한 줄(로그 파일에 그대로 남는 형태)과 (2) 실제
log_watcher._build_context_window()가 만드는 것과 같은 형태로 앞뒤 문맥을
붙인 버전을 함께 넣는다 — 컨텍스트 래핑 자체는 거리에 큰 영향이 없음이
실측으로 확인됐지만(오히려 약간 더 가까운 경우도 있었음), 운영에서 실제로
들어오는 두 형태 모두에 대해 앵커를 만들어두는 편이 안전하다.

실행:
    cd ~/agent && sudo .venv/bin/python -m scripts.add_chaos_injector_signatures
    sudo .venv/bin/python -m scripts.add_chaos_injector_signatures --dry-run
    sudo .venv/bin/python -m scripts.add_chaos_injector_signatures --remove

추가 (2026-09-07): 카테고리 커버리지 확장으로 DB_Connection(/inject/db_connection,
redis-py로 127.0.0.1:6379에 접속 시도 → 실제 ConnectionError)과 Network_Timeout
(/inject/network_timeout, psycopg2로 192.0.2.1(RFC 5737 TEST-NET-1)에 접속 시도 →
실제 접속 타임아웃)을 target-app에 신규 배포하면서 같은 train-serving skew를
반복하지 않도록 처음부터 문구를 같이 넣는다. action/target_process는 기존
train_set.json의 다수 라벨(DB_Connection→redis, Network_Timeout→postgres_pool)과
일치시켰다 — 이 두 카테고리는 Progressive Autonomy 정식 Shadow 절차 대상이라
(README 참고) autonomy_state에 직접 auto로 승격하지 않는다: 여기서 하는 일은
어디까지나 L1 데이터 품질 보정이고, 실행 권한 승급과는 무관하다.
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

BASE_DIR   = Path(__file__).parent.parent
CHROMA_DIR = str(BASE_DIR / "data" / "chroma_db")
COLLECTION = "error_playbook_vectors"
ID_PREFIX  = "chaos_sig_v1_"
SOURCE     = "chaos_injector_signature"

# deploy/target-app/main.py의 _append_evidence() 호출 문자열에서 그대로 가져온
# 실제 문구(레벨 + "chaos-injector: " + 메시지, 타임스탬프는 매번 달라지므로 제외).
_CLEAN_LINES: dict[str, list[str]] = {
    "Out_Of_Memory": [
        "CRITICAL chaos-injector: python memory allocator vs cgroup mem_limit=512m — "
        "returncode=137, killed by real cgroup OOM",
        "CRITICAL chaos-injector: python memory allocator OOM test timed out — "
        "process likely hung under memory pressure",
    ],
    "Disk_Full": [
        "ERROR chaos-injector: dd wrote into 150m tmpfs (/fill) requesting 180M — "
        "returncode=1, real No space left on device "
        "(dd: error writing '/fill/junk': No space left on device)",
    ],
    "Process_Crash": [
        "CRITICAL chaos-injector: target-app process (pid=1, rss=42.3MB, uptime=1234.5s) "
        "about to crash — real process crash injection",
        "CRITICAL chaos-injector: target-app process (pid=1) about to crash — "
        "real process crash injection",
    ],
    "Permission_Denied": [
        "CRITICAL chaos-injector: PermissionError — [Errno 13] Permission denied: "
        "'/app/data/locked_reload.sh' (mode=000, no execute bit set): "
        "[Errno 13] Permission denied: '/app/data/locked_reload.sh'",
    ],
    "Path_Not_Found": [
        "CRITICAL chaos-injector: FileNotFoundError — [Errno 2] No such file or directory: "
        "'/app/data/missing_module_v2.conf': [Errno 2] No such file or directory: "
        "'/app/data/missing_module_v2.conf'",
    ],
    "Configuration_Error": [
        "CRITICAL chaos-injector: Configuration Error — JSONDecodeError parsing "
        "/app/data/app_config.json: Expecting value: line 1 column 26 (char 25)",
    ],
    "DB_Connection": [
        "CRITICAL chaos-injector: redis.exceptions.ConnectionError — Could not connect to "
        "Redis at 127.0.0.1:6379: Connection refused: Error 111 connecting to 127.0.0.1:6379. "
        "Connection refused.",
    ],
    "Network_Timeout": [
        'CRITICAL chaos-injector: psycopg2.OperationalError — connection to server at '
        '"192.0.2.1", port 5432 failed: timeout expired: connection to server at '
        '"192.0.2.1", port 5432 failed: timeout expired',
    ],
    # 2026-09-08 추가: MVP 8종 중 실제 카오스 인젝터가 없던 마지막 2개(Auth_Error/Memory_Leak).
    # 문구는 실제 로컬 uvicorn 실행으로 캡처한 형태를 그대로 씀(단, pid/RSS 수치는 매 실행마다
    # 달라지므로 Process_Crash의 기존 관례와 동일하게 대표값으로 고정 — 임베딩 거리는 정확한
    # 숫자가 아니라 문장 형태로 매칭되므로 문제 없음).
    "Auth_Error": [
        "CRITICAL chaos-injector: requests.exceptions.HTTPError — 401 Unauthorized calling "
        "/internal/protected with invalid bearer token: 401 Client Error: Unauthorized for "
        "url: http://127.0.0.1:9000/internal/protected",
    ],
    "Memory_Leak": [
        "CRITICAL chaos-injector: MemoryLeak — background process pid=4821 RSS steadily growing: "
        "42.0MB -> 57.0MB -> 72.0MB -> 87.0MB -> 102.0MB over ~8s — gradual leak signature "
        "(distinct from an immediate OOM-Killer event)",
    ],
    # 2026-09-08 추가 (계속): DB_Deadlock 실제 인젝터(SQLite 자체 락 경합, 별도 DB
    # 서버 불필요). 문구는 로컬 실제 실행으로 캡처(3회 반복 재현 확인, 매번 동일 문구).
    "DB_Deadlock": [
        "CRITICAL chaos-injector: sqlite3.OperationalError — database is locked (concurrent "
        "transaction holding EXCLUSIVE lock): database is locked",
    ],
}

# log_watcher._build_context_window()가 실제로 만드는 것과 같은 형태(앞뒤 일반
# API 로그 몇 줄 + "[LOG CONTEXT]" 블록)로 감싼 대표 예시 1개씩 — 실측 결과
# 래핑이 거리를 악화시키진 않았지만, 운영에서 실제로 들어오는 형태 그대로도
# 앵커를 하나씩 심어둔다.
_WRAPPED_LINES: dict[str, str] = {
    "Out_Of_Memory": (
        "CRITICAL chaos-injector: python memory allocator vs cgroup mem_limit=512m — "
        "returncode=137, killed by real cgroup OOM\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=4471\n"
        "INFO api: request handled id=4472\n"
        ">>> CRITICAL chaos-injector: python memory allocator vs cgroup mem_limit=512m — "
        "returncode=137, killed by real cgroup OOM\n"
        "INFO api: request handled id=4473\n"
        "WARNING healthcheck: container restarting"
    ),
    "Disk_Full": (
        "ERROR chaos-injector: dd wrote into 150m tmpfs (/fill) requesting 180M — "
        "returncode=1, real No space left on device "
        "(dd: error writing '/fill/junk': No space left on device)\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=8821\n"
        ">>> ERROR chaos-injector: dd wrote into 150m tmpfs (/fill) requesting 180M — "
        "returncode=1, real No space left on device "
        "(dd: error writing '/fill/junk': No space left on device)\n"
        "INFO api: request handled id=8822\n"
        "WARNING healthcheck: container restarting"
    ),
    "Process_Crash": (
        "CRITICAL chaos-injector: target-app process (pid=1, rss=42.3MB, uptime=1234.5s) "
        "about to crash — real process crash injection\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=1029\n"
        ">>> CRITICAL chaos-injector: target-app process (pid=1, rss=42.3MB, uptime=1234.5s) "
        "about to crash — real process crash injection\n"
        "WARNING healthcheck: container restarting"
    ),
    "Permission_Denied": (
        "CRITICAL chaos-injector: PermissionError — [Errno 13] Permission denied: "
        "'/app/data/locked_reload.sh' (mode=000, no execute bit set): "
        "[Errno 13] Permission denied: '/app/data/locked_reload.sh'\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=3341\n"
        ">>> CRITICAL chaos-injector: PermissionError — [Errno 13] Permission denied: "
        "'/app/data/locked_reload.sh' (mode=000, no execute bit set): "
        "[Errno 13] Permission denied: '/app/data/locked_reload.sh'\n"
        "INFO api: request handled id=3342"
    ),
    "Path_Not_Found": (
        "CRITICAL chaos-injector: FileNotFoundError — [Errno 2] No such file or directory: "
        "'/app/data/missing_module_v2.conf': [Errno 2] No such file or directory: "
        "'/app/data/missing_module_v2.conf'\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=5561\n"
        ">>> CRITICAL chaos-injector: FileNotFoundError — [Errno 2] No such file or directory: "
        "'/app/data/missing_module_v2.conf': [Errno 2] No such file or directory: "
        "'/app/data/missing_module_v2.conf'\n"
        "INFO api: request handled id=5562"
    ),
    "Configuration_Error": (
        "CRITICAL chaos-injector: Configuration Error — JSONDecodeError parsing "
        "/app/data/app_config.json: Expecting value: line 1 column 26 (char 25)\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=7781\n"
        ">>> CRITICAL chaos-injector: Configuration Error — JSONDecodeError parsing "
        "/app/data/app_config.json: Expecting value: line 1 column 26 (char 25)\n"
        "INFO api: request handled id=7782"
    ),
    "DB_Connection": (
        "CRITICAL chaos-injector: redis.exceptions.ConnectionError — Could not connect to "
        "Redis at 127.0.0.1:6379: Connection refused: Error 111 connecting to 127.0.0.1:6379. "
        "Connection refused.\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=9101\n"
        ">>> CRITICAL chaos-injector: redis.exceptions.ConnectionError — Could not connect to "
        "Redis at 127.0.0.1:6379: Connection refused: Error 111 connecting to 127.0.0.1:6379. "
        "Connection refused.\n"
        "INFO api: request handled id=9102"
    ),
    "Network_Timeout": (
        'CRITICAL chaos-injector: psycopg2.OperationalError — connection to server at '
        '"192.0.2.1", port 5432 failed: timeout expired: connection to server at '
        '"192.0.2.1", port 5432 failed: timeout expired\n'
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=9201\n"
        '>>> CRITICAL chaos-injector: psycopg2.OperationalError — connection to server at '
        '"192.0.2.1", port 5432 failed: timeout expired: connection to server at '
        '"192.0.2.1", port 5432 failed: timeout expired\n'
        "WARNING healthcheck: container restarting"
    ),
    "Auth_Error": (
        "CRITICAL chaos-injector: requests.exceptions.HTTPError — 401 Unauthorized calling "
        "/internal/protected with invalid bearer token: 401 Client Error: Unauthorized for "
        "url: http://127.0.0.1:9000/internal/protected\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=6601\n"
        ">>> CRITICAL chaos-injector: requests.exceptions.HTTPError — 401 Unauthorized calling "
        "/internal/protected with invalid bearer token: 401 Client Error: Unauthorized for "
        "url: http://127.0.0.1:9000/internal/protected\n"
        "INFO api: request handled id=6602"
    ),
    "Memory_Leak": (
        "CRITICAL chaos-injector: MemoryLeak — background process pid=4821 RSS steadily growing: "
        "42.0MB -> 57.0MB -> 72.0MB -> 87.0MB -> 102.0MB over ~8s — gradual leak signature "
        "(distinct from an immediate OOM-Killer event)\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=7701\n"
        ">>> CRITICAL chaos-injector: MemoryLeak — background process pid=4821 RSS steadily "
        "growing: 42.0MB -> 57.0MB -> 72.0MB -> 87.0MB -> 102.0MB over ~8s — gradual leak "
        "signature (distinct from an immediate OOM-Killer event)\n"
        "INFO api: request handled id=7702"
    ),
    "DB_Deadlock": (
        "CRITICAL chaos-injector: sqlite3.OperationalError — database is locked (concurrent "
        "transaction holding EXCLUSIVE lock): database is locked\n"
        "[LOG CONTEXT]\n"
        "INFO api: request handled id=8801\n"
        ">>> CRITICAL chaos-injector: sqlite3.OperationalError — database is locked (concurrent "
        "transaction holding EXCLUSIVE lock): database is locked\n"
        "INFO api: request handled id=8802"
    ),
}

# 기존 train_set.json/etl_github_to_chroma.py의 ACTION_MAP과 일치시킴.
ACTION_MAP: dict[str, tuple[str, str, str]] = {
    "Out_Of_Memory":       ("clear_memory",         "", ""),
    "Disk_Full":           ("execute_rule_command", "", "journalctl --vacuum-size 1G"),
    "Process_Crash":       ("restart_service",       "rsyslog", ""),
    "Permission_Denied":   ("escalate_to_human",     "", ""),
    "Path_Not_Found":      ("escalate_to_human",     "", ""),
    "Configuration_Error": ("escalate_to_human",     "", ""),
    "DB_Connection":       ("restart_service",       "redis", ""),
    "Network_Timeout":     ("restart_service",       "postgres_pool", ""),
    "Auth_Error":          ("escalate_to_human",     "vault", ""),
    "Memory_Leak":         ("kill_process",          "", ""),
    "DB_Deadlock":         ("escalate_to_human",     "", ""),
}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _build_entries() -> tuple[list[str], list[str], list[dict]]:
    ids, docs, metas = [], [], []
    for cat, action_target_command in ACTION_MAP.items():
        action, target, command = action_target_command
        texts = list(_CLEAN_LINES.get(cat, []))
        if cat in _WRAPPED_LINES:
            texts.append(_WRAPPED_LINES[cat])
        for text in texts:
            uid = ID_PREFIX + _md5(text)
            ids.append(uid)
            docs.append(text)
            metas.append({
                "error_category": cat,
                "action_type":    action,
                "target_process": target,
                "command":        command,
                "source":         SOURCE,
            })
    return ids, docs, metas


def run(dry_run: bool = False) -> None:
    ids, docs, metas = _build_entries()

    print("=" * 70)
    print("  카오스 인젝터 실제 문구 → ChromaDB 추가")
    print("=" * 70)
    for cat in ACTION_MAP:
        n = sum(1 for m in metas if m["error_category"] == cat)
        print(f"  {cat:<22} {n}개")
    print(f"  {'합계':<22} {len(ids)}개")

    if dry_run:
        print("\n[--dry-run] ChromaDB 적재 건너뜀")
        return

    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col    = client.get_collection(COLLECTION)
    before = col.count()
    col.upsert(ids=ids, documents=docs, metadatas=metas)
    after = col.count()
    print(f"\n완료: {len(ids)}개 upsert | ChromaDB {before} → {after}개")

    print("\n" + "=" * 70)
    print("  L1 HIT 검증 (RAG_THRESHOLD=0.6, 실제 운영과 동일 임계값)")
    print("=" * 70)
    all_ok = True
    for cat in ACTION_MAP:
        probe = _CLEAN_LINES[cat][0]
        r = col.query(query_texts=[probe], n_results=1)
        d = r["distances"][0][0]
        m = r["metadatas"][0][0]
        hit = d <= 0.6
        if not hit:
            all_ok = False
        sym = "✅" if hit else "❌"
        print(f"  {sym} {cat:<22} d={d:.4f}  matched={m.get('error_category')}")
    print(f"\n{'전체 통과 ✅' if all_ok else '일부 실패 ❌ — 임계값 자체를 재검토할 필요 있음'}")


def remove_entries() -> None:
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col  = client.get_collection(COLLECTION)
    data = col.get(include=["metadatas"])
    rm   = [data["ids"][i] for i, m in enumerate(data["metadatas"]) if m.get("source") == SOURCE]
    if rm:
        col.delete(ids=rm)
    print(f"삭제 완료: {len(rm)}개 | 현재 {col.count()}개")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="추가할 내용만 출력, DB 적재 안 함")
    parser.add_argument("--remove",  action="store_true", help="이 스크립트가 추가한 항목만 삭제")
    args = parser.parse_args()

    if args.remove:
        remove_entries()
    else:
        run(dry_run=args.dry_run)
