"""
GitHub 공식 레포 Issues → ChromaDB 직접 적재 (PostgreSQL 불필요)
=================================================================
카테고리별 공식 레포 3개 × 10개 카테고리 = 30개 쿼리
이슈 본문에서 에러 스니펫을 regex로 추출 → ChromaDB upsert

실행:
    /root/agent/.venv/bin/python3.10 scripts/etl_github_to_chroma.py
    /root/agent/.venv/bin/python3.10 scripts/etl_github_to_chroma.py --dry-run
    /root/agent/.venv/bin/python3.10 scripts/etl_github_to_chroma.py --remove
    /root/agent/.venv/bin/python3.10 scripts/etl_github_to_chroma.py --stats
"""
import argparse, hashlib, json, logging, os, re, sys, time
from collections import defaultdict, Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request as _urllib
    HAS_REQUESTS = False

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 경로 ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
CHROMA_DIR = str(BASE_DIR / "data" / "chroma_db")
COLLECTION = "error_playbook_vectors"
ID_PREFIX  = "github_v2_"
SOURCE     = "github_v2"

# ── 카테고리별 액션 (executor.py ALLOWED_COMMANDS 기준) ──────────────────────
ACTION_MAP = {
    "Out_Of_Memory":      ("clear_memory",          "",        ""),
    "Memory_Leak":        ("clear_memory",          "",        ""),
    "Disk_Full":          ("execute_rule_command",  "",        "journalctl --vacuum-size 1G"),
    "Process_Crash":      ("restart_service",       "rsyslog", ""),
    "Network_Timeout":    ("execute_rule_command",  "",        "ss -tuln"),
    "DB_Connection":      ("execute_rule_command",  "",        "free -h"),
    "Auth_Error":         ("escalate_to_human",     "",        ""),
    "Permission_Denied":  ("escalate_to_human",     "",        ""),
    "Port_Conflict":      ("execute_rule_command",  "",        "ss -tuln"),
    "Configuration_Error":("escalate_to_human",     "",        ""),
}

# ── GitHub 검색 쿼리 (카테고리당 3개 공식 레포) ───────────────────────────────
QUERIES = [
    # ── Out_Of_Memory ────────────────────────────────────────────────────────
    {
        "cat": "Out_Of_Memory",
        "name": "PyTorch CUDA OOM",
        "q": 'repo:pytorch/pytorch "CUDA out of memory" is:closed label:bug',
    },
    {
        "cat": "Out_Of_Memory",
        "name": "TensorFlow OOM",
        "q": 'repo:tensorflow/tensorflow "ResourceExhaustedError" OR "OOM" is:closed label:type:bug',
    },
    {
        "cat": "Out_Of_Memory",
        "name": "JVM OutOfMemoryError",
        "q": 'repo:elastic/elasticsearch "OutOfMemoryError" OR "heap space" is:closed label:bug',
    },

    # ── Memory_Leak ──────────────────────────────────────────────────────────
    {
        "cat": "Memory_Leak",
        "name": "Node.js Memory Leak",
        "q": 'repo:nodejs/node "memory leak" OR "memory growing" is:closed label:bug',
    },
    {
        "cat": "Memory_Leak",
        "name": "Elasticsearch Memory Leak",
        "q": 'repo:elastic/elasticsearch "memory leak" OR "heap growing" is:closed',
    },
    {
        "cat": "Memory_Leak",
        "name": "Go Runtime Memory Leak",
        "q": 'repo:golang/go "memory leak" OR "goroutine leak" is:closed label:NeedsInvestigation',
    },

    # ── Disk_Full ────────────────────────────────────────────────────────────
    {
        "cat": "Disk_Full",
        "name": "Docker no space left",
        "q": 'repo:docker/compose "no space left on device" OR "ENOSPC" is:closed',
    },
    {
        "cat": "Disk_Full",
        "name": "Kubernetes disk pressure",
        "q": 'repo:kubernetes/kubernetes "disk pressure" OR "no space left" is:closed label:kind/bug',
    },
    {
        "cat": "Disk_Full",
        "name": "Elasticsearch disk watermark",
        "q": 'repo:elastic/elasticsearch "disk watermark" OR "flood stage" is:closed',
    },

    # ── Process_Crash ────────────────────────────────────────────────────────
    {
        "cat": "Process_Crash",
        "name": "Celery worker crash",
        "q": 'repo:celery/celery "worker exited" OR "Segmentation fault" OR "SIGSEGV" is:closed',
    },
    {
        "cat": "Process_Crash",
        "name": "Airflow task crash",
        "q": 'repo:apache/airflow "Task failed" OR "process killed" OR "core dumped" is:closed label:type:bug',
    },
    {
        "cat": "Process_Crash",
        "name": "Gunicorn worker crash",
        "q": 'repo:benoitc/gunicorn "worker timeout" OR "exited with signal" OR "SIGKILL" is:closed',
    },

    # ── DB_Connection ─────────────────────────────────────────────────────────
    {
        "cat": "DB_Connection",
        "name": "SQLAlchemy pool exhausted",
        "q": 'repo:sqlalchemy/sqlalchemy "pool" AND ("timeout" OR "exhausted" OR "QueuePool") is:closed',
    },
    {
        "cat": "DB_Connection",
        "name": "psycopg2 connection error",
        "q": 'repo:psycopg/psycopg2 "connection" AND ("refused" OR "timeout" OR "too many") is:closed',
    },
    {
        "cat": "DB_Connection",
        "name": "Redis connection refused",
        "q": 'repo:redis/redis-py "ConnectionError" OR "Connection refused" OR "max clients" is:closed',
    },

    # ── Network_Timeout ───────────────────────────────────────────────────────
    {
        "cat": "Network_Timeout",
        "name": "requests ConnectTimeout",
        "q": 'repo:psf/requests "ConnectTimeout" OR "ReadTimeout" OR "timed out" is:closed label:Bug',
    },
    {
        "cat": "Network_Timeout",
        "name": "aiohttp timeout",
        "q": 'repo:aio-libs/aiohttp "ServerTimeoutError" OR "asyncio.TimeoutError" OR "timeout" is:closed label:bug',
    },
    {
        "cat": "Network_Timeout",
        "name": "gRPC deadline exceeded",
        "q": 'repo:grpc/grpc "DEADLINE_EXCEEDED" OR "deadline exceeded" is:closed',
    },

    # ── Auth_Error ────────────────────────────────────────────────────────────
    {
        "cat": "Auth_Error",
        "name": "Vault authentication failed",
        "q": 'repo:hashicorp/vault "authentication failed" OR "permission denied" OR "invalid token" is:closed',
    },
    {
        "cat": "Auth_Error",
        "name": "Paramiko SSH auth failure",
        "q": 'repo:paramiko/paramiko "Authentication failed" OR "No authentication methods" is:closed',
    },
    {
        "cat": "Auth_Error",
        "name": "Django auth error",
        "q": 'repo:django/django "authentication" AND ("failed" OR "invalid" OR "forbidden") is:closed',
    },

    # ── Permission_Denied ────────────────────────────────────────────────────
    {
        "cat": "Permission_Denied",
        "name": "Ansible permission denied",
        "q": 'repo:ansible/ansible "Permission denied" OR "EACCES" OR "Operation not permitted" is:closed',
    },
    {
        "cat": "Permission_Denied",
        "name": "Docker socket permission",
        "q": 'repo:docker/docker-ce "permission denied" AND "docker.sock" OR "Cannot connect to the Docker daemon" is:closed',
    },
    {
        "cat": "Permission_Denied",
        "name": "Kubernetes RBAC forbidden",
        "q": 'repo:kubernetes/kubernetes "is forbidden" OR "RBAC" OR "cannot get" is:closed label:kind/bug',
    },

    # ── Port_Conflict ────────────────────────────────────────────────────────
    {
        "cat": "Port_Conflict",
        "name": "Docker port already allocated",
        "q": 'repo:docker/compose "port is already allocated" OR "address already in use" OR "EADDRINUSE" is:closed',
    },
    {
        "cat": "Port_Conflict",
        "name": "Traefik bind failed",
        "q": 'repo:traefik/traefik "address already in use" OR "bind" AND "failed" is:closed label:kind/bug',
    },
    {
        "cat": "Port_Conflict",
        "name": "Kubernetes port conflict",
        "q": 'repo:kubernetes/kubernetes "already in use" OR "hostPort" AND "conflict" is:closed label:kind/bug',
    },

    # ── Configuration_Error ───────────────────────────────────────────────────
    {
        "cat": "Configuration_Error",
        "name": "Django ImproperlyConfigured",
        "q": 'repo:django/django "ImproperlyConfigured" OR "configuration error" is:closed label:Bug',
    },
    {
        "cat": "Configuration_Error",
        "name": "Spring Boot config failure",
        "q": 'repo:spring-projects/spring-boot "Failed to configure" OR "could not resolve placeholder" is:closed label:type:bug',
    },
    {
        "cat": "Configuration_Error",
        "name": "Helm/K8s invalid config",
        "q": 'repo:helm/helm "invalid" AND ("values" OR "configuration" OR "yaml") is:closed label:bug',
    },
]

# ── 에러 스니펫 추출 패턴 ────────────────────────────────────────────────────
_ERROR_PATTERNS = [
    # 코드블록 내 에러 (```...``` 또는 ~~~...~~~)
    re.compile(r'```(?:\w+\n)?(.*?)```', re.DOTALL),
    re.compile(r'~~~(?:\w+\n)?(.*?)~~~', re.DOTALL),
    # 들여쓰기 코드 (4칸)
    re.compile(r'(?:^    .+\n?)+', re.MULTILINE),
    # 에러 키워드로 시작하는 줄
    re.compile(
        r'^.{0,30}(?:Error|Exception|FATAL|CRITICAL|Traceback|Caused by|'
        r'errno|ENOSPC|ECONNREFUSED|EADDRINUSE|SIGKILL|SIGSEGV|'
        r'OOM|OutOfMemory|PermissionError|ConnectionRefused|Timeout).+',
        re.IGNORECASE | re.MULTILINE,
    ),
]

_ERROR_KW = re.compile(
    r'error|exception|fatal|critical|traceback|oom|timeout|'
    r'refused|denied|enospc|eaddrinuse|sigsegv|sigkill|'
    r'no space|out of memory|connection|permission',
    re.IGNORECASE,
)

_NOISE_RE = re.compile(
    r'(<!--.*?-->|!\[.*?\]\(.*?\)|https?://\S+|'
    r'<[^>]+>|\*\*.*?\*\*|^\s*[-*>#+]\s*)',
    re.DOTALL | re.MULTILINE,
)


def extract_error_snippet(body: str, max_len: int = 400) -> str | None:
    """이슈 본문에서 에러 스니펫 추출. 없으면 None."""
    if not body:
        return None

    # 마크다운 노이즈 제거
    clean = _NOISE_RE.sub(' ', body)
    clean = re.sub(r'\s+', ' ', clean).strip()

    candidates = []

    for pat in _ERROR_PATTERNS:
        for m in pat.finditer(body):
            snippet = m.group(0 if pat.groups == 0 else 1).strip()
            if len(snippet) < 20:
                continue
            if _ERROR_KW.search(snippet):
                candidates.append(snippet)

    if candidates:
        # 가장 짧고 에러 키워드 밀도 높은 스니펫 선택
        candidates.sort(key=lambda s: len(s))
        best = candidates[0][:max_len].strip()
        # 코드블록 안의 에러 줄만 추출
        lines = [l.strip() for l in best.splitlines() if _ERROR_KW.search(l)]
        if lines:
            return ' | '.join(lines[:3])
        return best

    # 폴백: 에러 키워드 포함 줄 최대 2개
    lines = [l.strip() for l in clean.splitlines() if _ERROR_KW.search(l) and len(l.strip()) > 20]
    if lines:
        return ' | '.join(lines[:2])[:max_len]

    return None


# ── GitHub API ────────────────────────────────────────────────────────────────
def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _github_get(url: str, token: str) -> dict | None:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    for attempt in range(3):
        try:
            if HAS_REQUESTS:
                resp = _requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (403, 429):
                    retry = int(resp.headers.get("Retry-After", 60))
                    log.warning(f"Rate limit — {retry}s 대기")
                    time.sleep(retry)
                    continue
                if resp.status_code == 401:
                    log.warning("토큰 인증 실패 — 비인증 모드로 재시도")
                    headers.pop("Authorization", None)
                    token = ""
                    continue
                if resp.status_code == 422:
                    log.warning(f"검색 쿼리 오류 (422): {url}")
                    return None
                log.warning(f"HTTP {resp.status_code}: {url}")
                return None
            else:
                import urllib.parse, urllib.request as ur
                req = ur.Request(url, headers=headers)
                with ur.urlopen(req, timeout=15) as r:
                    return json.load(r)
        except Exception as e:
            log.warning(f"요청 실패 (시도 {attempt+1}/3): {e}")
            time.sleep(5 * (attempt + 1))
    return None


def fetch_issues(query_cfg: dict, token: str, per_page: int = 50) -> list[str]:
    """GitHub Search API → 에러 스니펫 리스트 반환."""
    import urllib.parse
    q   = query_cfg["q"]
    url = f"https://api.github.com/search/issues?q={urllib.parse.quote(q)}&per_page={per_page}&sort=updated"
    data = _github_get(url, token)
    if not data:
        return []

    snippets = []
    seen: set[str] = set()
    for item in data.get("items", []):
        # 이슈 제목 + 본문 모두 시도
        sources = [item.get("body", ""), item.get("title", "")]
        for src in sources:
            snippet = extract_error_snippet(src)
            if snippet and snippet not in seen and len(snippet) > 20:
                seen.add(snippet)
                snippets.append(snippet)
                break  # 이슈당 1개

    log.info(f"  [{query_cfg['name']}] {len(data.get('items',[]))}개 이슈 → {len(snippets)}개 스니펫")
    return snippets


# ── 메인 ──────────────────────────────────────────────────────────────────────
def run(dry_run: bool = False, per_page: int = 50):
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        log.warning("GITHUB_TOKEN 미설정 — 비인증 모드로 실행 (rate limit: 10 req/min)")
    else:
        log.info(f"GITHUB_TOKEN 로드됨 ({len(token)}자)")

    # ChromaDB
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col    = client.get_collection(COLLECTION)
    before = col.count()

    collected: dict[str, list[str]] = defaultdict(list)  # cat → snippets

    print(f"\n{'='*60}")
    print(f" GitHub 공식 레포 크롤링 ({len(QUERIES)}개 쿼리, per_page={per_page})")
    print(f"{'='*60}")

    for i, qcfg in enumerate(QUERIES, 1):
        cat = qcfg["cat"]
        print(f"\n[{i:02d}/{len(QUERIES)}] {cat} — {qcfg['name']}")
        snippets = fetch_issues(qcfg, token, per_page=per_page)
        collected[cat].extend(snippets)
        # 비인증: 10 req/min → 7s 간격, 인증: 30 req/min → 2s 간격
        time.sleep(2 if token else 7)

    # 결과 요약
    print(f"\n{'='*60}")
    print(f" 수집 결과")
    print(f"{'='*60}")
    total = 0
    for cat in ACTION_MAP:
        cnt = len(collected.get(cat, []))
        total += cnt
        print(f"  {cat:<25} {cnt:>4}개")
    print(f"  {'합계':<25} {total:>4}개")

    if dry_run:
        print("\n[--dry-run] ChromaDB 적재 건너뜀")
        return

    # ChromaDB upsert
    seen_ids: set[str] = set()
    ids, docs, metas = [], [], []

    for cat, snippets in collected.items():
        action, target, command = ACTION_MAP[cat]
        for text in snippets:
            uid = ID_PREFIX + _md5(text)
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            ids.append(uid)
            docs.append(text)
            metas.append({
                "error_category": cat,
                "action_type":    action,
                "target_process": target,
                "command":        command,
                "source":         SOURCE,
            })

    batch = 100
    upserted = 0
    for i in range(0, len(ids), batch):
        col.upsert(
            ids=ids[i:i+batch],
            documents=docs[i:i+batch],
            metadatas=metas[i:i+batch],
        )
        upserted += len(ids[i:i+batch])

    after = col.count()
    print(f"\n완료: {upserted}개 upsert | ChromaDB {before} → {after}개")

    # 검증
    print(f"\n{'='*60}")
    print(f" L1 HIT 검증 (threshold=1.2)")
    print(f"{'='*60}")
    probes = {
        "Out_Of_Memory":      "CUDA out of memory. Tried to allocate 2.50 GiB",
        "Memory_Leak":        "memory leak goroutine heap growing unbounded RSS",
        "Disk_Full":          "no space left on device ENOSPC disk full",
        "Process_Crash":      "worker exited with signal SIGSEGV segmentation fault core dumped",
        "Network_Timeout":    "ConnectTimeout ReadTimeout deadline exceeded ETIMEDOUT",
        "DB_Connection":      "QueuePool limit connection pool exhausted too many connections",
        "Auth_Error":         "authentication failed invalid token permission denied vault",
        "Permission_Denied":  "Permission denied EACCES operation not permitted docker.sock",
        "Port_Conflict":      "address already in use EADDRINUSE bind failed port",
        "Configuration_Error":"ImproperlyConfigured could not resolve placeholder invalid config",
    }
    all_ok = True
    for cat, probe in probes.items():
        r = col.query(query_texts=[probe], n_results=1)
        d = r["distances"][0][0]
        m = r["metadatas"][0][0]
        hit = d < 1.2
        if not hit:
            all_ok = False
        sym = "✅" if hit else "❌"
        print(f"  {sym} {cat:<25} d={d:.3f}  src={m.get('source','?')[:10]}")
    print(f"\n{'전체 통과 ✅' if all_ok else '일부 실패 ❌'}")


def remove_entries():
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col  = client.get_collection(COLLECTION)
    data = col.get(include=["metadatas"])
    rm   = [data["ids"][i] for i, m in enumerate(data["metadatas"])
            if m.get("source") == SOURCE]
    if rm:
        col.delete(ids=rm)
    print(f"삭제 완료: {len(rm)}개 | 현재 {col.count()}개")


def show_stats():
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col  = client.get_collection(COLLECTION)
    data = col.get(include=["metadatas"])
    src_counts = Counter(m.get("source", "?") for m in data["metadatas"])
    cat_counts = Counter(m.get("error_category", "?") for m in data["metadatas"])
    print(f"\n총 {col.count()}개")
    print("\n[소스별]")
    for src, cnt in sorted(src_counts.items()):
        print(f"  {src:<25} {cnt:>5}개")
    print("\n[카테고리별]")
    for cat, cnt in sorted(cat_counts.items()):
        print(f"  {cat:<25} {cnt:>5}개")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",   action="store_true", help="크롤링만, DB 적재 안함")
    parser.add_argument("--remove",    action="store_true", help="github_v2 항목 삭제")
    parser.add_argument("--stats",     action="store_true", help="ChromaDB 현황 출력")
    parser.add_argument("--per-page",  type=int, default=50, help="쿼리당 이슈 수 (최대 100)")
    args = parser.parse_args()

    if args.remove:
        remove_entries()
    elif args.stats:
        show_stats()
    else:
        run(dry_run=args.dry_run, per_page=args.per_page)
