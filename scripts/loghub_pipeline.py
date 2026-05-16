"""
Loghub → ChromaDB 자동 분류·적재 파이프라인
=============================================
1. Loghub GitHub에서 10개 데이터셋 다운로드
2. 키워드 1차 분류 (빠름, 80%+ 처리)
3. Ollama LLM(qwen2.5:0.5b) 2차 분류 (모호한 케이스) — --keyword-only 시 생략
4. 카테고리별 200개 상한으로 ChromaDB 적재

실행:
    python scripts/loghub_pipeline.py
    python scripts/loghub_pipeline.py --dry-run        # 다운로드/분류만, DB 적재 안함
    python scripts/loghub_pipeline.py --keyword-only   # LLM 없이 키워드 분류만 적재
    python scripts/loghub_pipeline.py --remove         # 이 스크립트로 추가한 항목 삭제
"""

import argparse, hashlib, json, os, re, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings
from rich.console import Console
from rich.progress import track
from rich.table import Table
from rich import box

console = Console()

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
CHROMA_DIR  = str(BASE_DIR / "data" / "chroma_db")
CACHE_DIR   = BASE_DIR / "data" / "loghub_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

COLLECTION_NAME = "error_playbook_vectors"
ID_PREFIX       = "loghub_v1_"
MAX_PER_CAT     = 200   # 카테고리당 최대 적재 수

# ── Loghub 데이터셋 목록 ──────────────────────────────────────────────────────
DATASETS = {
    "Linux":       "Linux_2k.log",
    "Apache":      "Apache_2k.log",
    "HDFS":        "HDFS_2k.log",
    "Hadoop":      "Hadoop_2k.log",
    "OpenStack":   "OpenStack_2k.log",
    "BGL":         "BGL_2k.log",
    "OpenSSH":     "OpenSSH_2k.log",
    "Spark":       "Spark_2k.log",
    "Thunderbird": "Thunderbird_2k.log",
    "Zookeeper":   "Zookeeper_2k.log",
}
LOGHUB_RAW = "https://raw.githubusercontent.com/logpai/loghub/master/{ds}/{fname}"

# ── 10개 카테고리 키워드 규칙 ─────────────────────────────────────────────────
# (패턴, 카테고리) — 순서대로 적용, 첫 매칭 사용
KEYWORD_RULES: list[tuple[list[str], str]] = [
    # ── Disk_Full (명확한 시스템 에러코드)
    (["no space left on device", "errno 28", "enospc", "disk full",
      "diskquota", "filesystem full", "volume full", "out of disk",
      "write failed.*space", "no space left"],
     "Disk_Full"),

    # ── Port_Conflict
    (["address already in use", "eaddrinuse", "bind.*failed.*address",
      "port.*already", "listen.*already in use", "socket.*in use"],
     "Port_Conflict"),

    # ── Out_Of_Memory (OOM killer, heap OOM)
    (["out of memory", "oom killer", "kill process.*score",
      "java.lang.outofmemoryerror", "gc overhead limit exceeded",
      "cannot allocate memory", "memory exhausted", "heap out of memory",
      "killed.*oom", "oom_kill", "memory limit exceeded",
      "bad_alloc", "std::bad_alloc"],
     "Out_Of_Memory"),

    # ── Memory_Leak
    (["memory leak", "heap growing", "rss grew", "rss growing",
      "memory pressure increasing", "unbounded growth", "heap unbounded",
      "memory usage.*climbing", "leak detected"],
     "Memory_Leak"),

    # ── Auth_Error (SSH/인증 실패)
    (["authentication failure", "authentication failed", "failed password",
      "invalid user", "invalid credentials", "unauthorized",
      "access denied.*user", "login failed", "bad password",
      "auth failure", "pam_unix.*failure", "permission denied.*ssh",
      "sshd.*invalid", "connection closed.*authenticating",
      "failed.*authentication"],
     "Auth_Error"),

    # ── Permission_Denied (파일시스템 권한)
    (["permission denied", "eacces", "errno 13", "operation not permitted",
      "cannot open.*permission", "access denied.*file",
      "no permission to", "permission error"],
     "Permission_Denied"),

    # ── Configuration_Error
    (["invalid configuration", "configuration error", "config.*error",
      "syntax error", "unknown directive", "parse error.*config",
      "improperlyconfigured", "missing required.*config",
      "invalid.*parameter", "bad configuration",
      "failed to parse config", "config.*failed"],
     "Configuration_Error"),

    # ── Process_Crash (signal/crash)
    (["segfault", "segmentation fault", "sigsegv", "sigabrt", "sigkill",
      "core dump", "core dumped", "exited with signal",
      "killed.*signal", "process.*crashed", "crash detected",
      "fatal.*crash", "jvm crash", "aborted.*core",
      "exited.*code=dumped", "fatal error.*abort"],
     "Process_Crash"),

    # ── DB_Connection (DB 전용 연결 실패)
    (["too many connections", "connection pool.*exhaust",
      "pool.*exhaust", "max.*connections.*reached",
      "could not connect.*database", "connection refused.*postgres",
      "connection refused.*mysql", "connection refused.*redis",
      "connection refused.*mongo", "db.*connection.*fail",
      "database.*connection.*timeout", "sql.*connection.*error"],
     "DB_Connection"),

    # ── Network_Timeout (일반 네트워크)
    (["connection timed out", "connection timeout", "network timeout",
      "read timeout", "connect timeout", "etimedout",
      "timed out.*connection", "upstream timed out",
      "deadline exceeded", "context deadline exceeded",
      "retries exhausted", "unreachable.*timeout",
      "socket.*timeout", "http.*timeout", "rpc.*deadline"],
     "Network_Timeout"),
]

CATEGORIES = [r[1] for r in KEYWORD_RULES]

# ── 카테고리별 액션 매핑 ──────────────────────────────────────────────────────
ACTION_MAP = {
    "Out_Of_Memory":     ("clear_memory",         "",        ""),
    "Memory_Leak":       ("clear_memory",         "",        ""),
    "Disk_Full":         ("execute_rule_command",  "",        "journalctl --vacuum-size 1G"),
    "Process_Crash":     ("restart_service",       "rsyslog", ""),
    "Network_Timeout":   ("execute_rule_command",  "",        "ss -tuln"),
    "DB_Connection":     ("execute_rule_command",  "",        "free -h"),
    "Auth_Error":        ("escalate_to_human",     "",        ""),
    "Permission_Denied": ("escalate_to_human",     "",        ""),
    "Port_Conflict":     ("execute_rule_command",  "",        "ss -tuln"),
    "Configuration_Error":("escalate_to_human",   "",        ""),
}


# ── 유틸 ─────────────────────────────────────────────────────────────────────
def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _clean_log(line: str) -> str:
    """Loghub 로그 포맷에서 실제 메시지 부분만 추출·정제."""
    line = line.strip()
    # BGL 형식: "- 1117838570 2005.06.03 ..." → 앞 숫자/날짜 제거
    line = re.sub(r'^-?\s+\d{9,10}\s+\d{4}\.\d{2}\.\d{2}\s+\S+\s+\S+\s+\S+\s+\S+\s+', '', line)
    # Thunderbird 형식 앞 필드 제거
    line = re.sub(r'^\d+\s+\d{4}\.\d{2}\.\d{2}\s+\S+\s+\S+\s+\S+\s+\S+\s+', '', line)
    # 타임스탬프 패턴 제거 (단, 로그 내용은 보존)
    line = re.sub(r'^\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+', '', line)  # Linux syslog
    line = re.sub(r'^\d{6}\s+\d{6}\s+\d+\s+\w+\s+', '', line)  # HDFS
    line = re.sub(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d+\s+\w+\s+', '', line)  # Hadoop/Spark
    line = re.sub(r'^\[.*?\]\s+', '', line)  # [timestamp] 제거
    return line.strip()


def keyword_classify(line: str) -> str | None:
    """키워드 기반 1차 분류. 매칭 안되면 None."""
    lower = line.lower()
    for patterns, cat in KEYWORD_RULES:
        for pat in patterns:
            if re.search(pat, lower):
                return cat
    return None


# ── Ollama LLM 분류 ───────────────────────────────────────────────────────────
OLLAMA_URL  = "http://localhost:11434/api/generate"
CATS_STR    = ", ".join(CATEGORIES)
BATCH_SIZE  = 8   # 한 번에 분류할 로그 수

LLM_PROMPT_TPL = """\
Classify each log line into exactly ONE of these categories:
{cats}

Rules:
- Out_Of_Memory: OOM killer, heap OOM, memory allocation failure
- Memory_Leak: memory growing unbounded, leak detected
- Disk_Full: no space left on device, disk capacity exceeded
- Process_Crash: segfault, signal kill, core dump, service crashed
- Network_Timeout: connection/read/write timeout, ETIMEDOUT, deadline exceeded
- DB_Connection: DB connection pool exhausted, too many connections
- Auth_Error: authentication/login failure, invalid credentials, SSH failure
- Permission_Denied: file permission error, EACCES, operation not permitted
- Port_Conflict: address already in use, EADDRINUSE, port binding failed
- Configuration_Error: config parse error, invalid setting, missing env var
- NONE: does not fit any category above

Respond with a JSON array of category strings, one per log line.
Example: ["Out_Of_Memory", "NONE", "Auth_Error"]

Log lines:
{logs}"""


def llm_classify_batch(lines: list[str]) -> list[str]:
    """Ollama로 배치 분류. 실패 시 'NONE' 반환."""
    numbered = "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines))
    prompt   = LLM_PROMPT_TPL.format(cats=CATS_STR, logs=numbered)
    payload  = json.dumps({
        "model": "qwen2.5:0.5b",
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 200},
    }).encode()

    try:
        req  = urllib.request.Request(OLLAMA_URL, data=payload,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.load(resp).get("response", "")
        # JSON 배열 추출
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            arr = json.loads(match.group())
            # 결과 개수가 맞지 않으면 보정
            while len(arr) < len(lines):
                arr.append("NONE")
            return [str(x) if x in CATEGORIES else "NONE" for x in arr[:len(lines)]]
    except Exception as e:
        console.print(f"[dim]LLM 오류: {e}[/]")
    return ["NONE"] * len(lines)


# ── 다운로드 ─────────────────────────────────────────────────────────────────
def download_dataset(ds_name: str, fname: str) -> Path:
    cache_path = CACHE_DIR / f"{ds_name}_{fname}"
    if cache_path.exists():
        return cache_path
    url = LOGHUB_RAW.format(ds=ds_name, fname=fname)
    console.print(f"  [cyan]다운로드:[/] {ds_name}/{fname} ...", end=" ")
    try:
        urllib.request.urlretrieve(url, cache_path)
        console.print(f"[green]완료[/] ({cache_path.stat().st_size//1024}KB)")
        return cache_path
    except Exception as e:
        console.print(f"[red]실패: {e}[/]")
        return None


# ── 메인 파이프라인 ───────────────────────────────────────────────────────────
def run_pipeline(dry_run: bool = False, keyword_only: bool = False):
    # ── 1. 다운로드 ────────────────────────────────────────────────────────────
    console.rule("[bold cyan]Step 1. Loghub 데이터셋 다운로드[/]")
    paths = {}
    for ds, fname in DATASETS.items():
        p = download_dataset(ds, fname)
        if p:
            paths[ds] = p

    # ── 2. 파싱 & 1차 키워드 분류 ──────────────────────────────────────────────
    console.rule("[bold cyan]Step 2. 키워드 1차 분류[/]")

    kw_hits:   dict[str, list[str]] = defaultdict(list)  # cat → [log_text]
    ambiguous: list[str]            = []                 # LLM 후보
    total_lines = 0

    for ds, path in paths.items():
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        total_lines += len(lines)
        for raw in lines:
            text = _clean_log(raw)
            if len(text) < 20:
                continue
            cat = keyword_classify(text)
            if cat:
                if len(kw_hits[cat]) < MAX_PER_CAT:
                    kw_hits[cat].append(text)
            else:
                if not keyword_only and len(ambiguous) < 3000:   # LLM 후보 상한
                    ambiguous.append(text)

    kw_total = sum(len(v) for v in kw_hits.values())
    console.print(f"  총 라인: {total_lines:,} | 키워드 히트: {kw_total:,} | LLM 후보: {len(ambiguous):,}")
    for cat in CATEGORIES:
        console.print(f"    {cat:<25} {len(kw_hits[cat]):>4}개")

    # ── 3. LLM 2차 분류 (--keyword-only 시 생략) ───────────────────────────────
    llm_hits: dict[str, list[str]] = defaultdict(list)

    if keyword_only:
        console.rule("[bold cyan]Step 3. LLM 분류 생략 (--keyword-only)[/]")
        console.print(f"  [yellow]키워드 분류만 사용: {kw_total}개[/]")
    else:
        console.rule("[bold cyan]Step 3. Ollama LLM 2차 분류[/]")

        # 아직 MAX_PER_CAT 미달인 카테고리만 보강
        need_cats = {cat for cat in CATEGORIES if len(kw_hits[cat]) < MAX_PER_CAT}

        if need_cats and ambiguous:
            console.print(f"  보강 필요 카테고리: {need_cats}")
            batches = [ambiguous[i:i+BATCH_SIZE] for i in range(0, len(ambiguous), BATCH_SIZE)]
            console.print(f"  LLM 배치: {len(batches)}회 (배치 크기={BATCH_SIZE})")

            for batch in track(batches, description="LLM 분류 중..."):
                results = llm_classify_batch(batch)
                for text, cat in zip(batch, results):
                    if cat in need_cats and len(kw_hits[cat]) + len(llm_hits[cat]) < MAX_PER_CAT:
                        llm_hits[cat].append(text)

                # 보강 완료된 카테고리 제거
                need_cats = {c for c in need_cats
                             if len(kw_hits[c]) + len(llm_hits[c]) < MAX_PER_CAT}
                if not need_cats:
                    console.print("  [green]모든 카테고리 보강 완료, 조기 종료[/]")
                    break

    # ── 4. 결과 합산 ───────────────────────────────────────────────────────────
    classified: dict[str, list[str]] = defaultdict(list)
    for cat in CATEGORIES:
        classified[cat] = kw_hits[cat] + llm_hits[cat]

    total_classified = sum(len(v) for v in classified.values())
    console.rule("[bold cyan]Step 4. 분류 결과[/]")
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("카테고리", style="cyan")
    table.add_column("키워드", justify="right")
    table.add_column("LLM", justify="right")
    table.add_column("합계", justify="right")
    table.add_column("액션")
    for cat in CATEGORIES:
        action, _, cmd = ACTION_MAP[cat]
        table.add_row(
            cat,
            str(len(kw_hits[cat])),
            str(len(llm_hits.get(cat, []))),
            str(len(classified[cat])),
            f"{action} {cmd}".strip(),
        )
    table.add_row("[bold]합계[/]", "", "", str(total_classified), "")
    console.print(table)

    if dry_run:
        console.print("[yellow]--dry-run: DB 적재 건너뜀[/]")
        return

    # ── 5. ChromaDB 적재 ───────────────────────────────────────────────────────
    console.rule("[bold cyan]Step 5. ChromaDB 적재[/]")
    client = chromadb.PersistentClient(
        path=CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )
    col    = client.get_collection(COLLECTION_NAME)
    before = col.count()

    seen_ids: set[str] = set()
    ids, docs, metas = [], [], []
    for cat, texts in classified.items():
        action, target, command = ACTION_MAP[cat]
        for text in texts:
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
                "source":         "loghub_v1",
            })

    # 배치 upsert
    batch = 100
    upserted = 0
    for i in range(0, len(ids), batch):
        col.upsert(ids=ids[i:i+batch], documents=docs[i:i+batch], metadatas=metas[i:i+batch])
        upserted += len(ids[i:i+batch])
        console.print(f"  적재 {upserted}/{len(ids)}...", end="\r")

    after = col.count()
    console.print(f"\n  [green]완료: {upserted}개 upsert | ChromaDB {before} → {after}개[/]")

    # ── 6. 최종 검증 ───────────────────────────────────────────────────────────
    console.rule("[bold cyan]Step 6. 최종 검증[/]")
    probes = {
        "Out_Of_Memory":     "Out of memory Kill process score OOM kernel",
        "Memory_Leak":       "heap growing unbounded memory leak RSS intervention",
        "Disk_Full":         "No space left on device disk full filesystem write failed",
        "Process_Crash":     "segfault SIGSEGV core dumped service crashed worker exited signal",
        "Network_Timeout":   "connection timed out upstream unreachable deadline exceeded",
        "DB_Connection":     "connection pool exhausted too many connections database refused",
        "Auth_Error":        "authentication failed invalid credentials repeated failure sshd",
        "Permission_Denied": "permission denied EACCES errno 13 cannot write file",
        "Port_Conflict":     "address already in use EADDRINUSE bind failed port 8080",
        "Configuration_Error":"invalid configuration syntax error unknown directive missing env",
    }
    vtable = Table(box=box.SIMPLE_HEAVY)
    vtable.add_column("카테고리", style="cyan")
    vtable.add_column("distance", justify="right")
    vtable.add_column("action")
    vtable.add_column("source")
    vtable.add_column("L1?", justify="center")

    all_ok = True
    for cat, probe in probes.items():
        r = col.query(query_texts=[probe], n_results=1)
        d = r["distances"][0][0]
        m = r["metadatas"][0][0]
        hit = d < 1.2
        if not hit:
            all_ok = False
        vtable.add_row(cat, f"{d:.3f}", m.get("action_type","?"),
                       m.get("source","?")[:12], "✅" if hit else "❌")
    console.print(vtable)
    console.print("[bold green]전체 통과 ✅[/]" if all_ok else "[bold red]일부 실패 ❌[/]")


def remove_entries():
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col  = client.get_collection(COLLECTION_NAME)
    data = col.get(include=["metadatas"])
    rm   = [data["ids"][i] for i, m in enumerate(data["metadatas"])
            if m.get("source") == "loghub_v1"]
    if rm:
        col.delete(ids=rm)
    console.print(f"[red]삭제 완료: {len(rm)}개 | 현재 {col.count()}개[/]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--keyword-only", action="store_true", help="LLM 없이 키워드 분류만 적재")
    parser.add_argument("--remove",       action="store_true")
    args = parser.parse_args()

    if args.remove:
        remove_entries()
    else:
        run_pipeline(dry_run=args.dry_run, keyword_only=args.keyword_only)
