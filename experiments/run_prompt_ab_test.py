"""
프롬프트 A/B/C 비교 실험
A: 현재 (few-shot 4개, system_context 마지막)
B: 시스템 상태 먼저 (system_context를 examples 앞에 배치)
C: few-shot 2개 (examples 절반)

평가 지표:
  format_ok   : 단일 줄, 마크다운 없음, sudo 없음
  cmd_valid   : 첫 토큰이 알려진 Linux 명령어
  aligned     : 에러 카테고리와 명령어의 의미 일치
  latency_ms  : Ollama 응답 시간

결과: experiments/results/prompt_ab_results_<ts>.csv
"""
import csv
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL    = "qwen2.5:0.5b"
TIMEOUT         = 60
SAMPLE_SIZE     = None  # main()에서 test_set.json 전체 크기로 동적 설정
MAX_LOG_CHARS   = 300  # LLM에 전달할 최대 로그 길이

TEST_SET_PATH = Path("data/test_set.json")
RESULTS_DIR   = Path("experiments/results")

# ── 카테고리별 기대 명령어 패턴 (aligned 판정용) ───────────────────────
CATEGORY_CMD_HINTS: dict[str, list[str]] = {
    "Out_Of_Memory":      ["pkill", "kill", "top", "free", "vmstat"],
    "Disk_Full":          ["df", "du", "rm", "find", "truncate"],
    "Network_Timeout":    ["ping", "ss", "netstat", "curl", "traceroute"],
    "Port_Conflict":      ["ss", "netstat", "lsof", "kill", "systemctl"],
    "Process_Crash":      ["systemctl", "journalctl", "kill", "pkill"],
    "DB_Connection":      ["systemctl", "ss", "ping", "psql", "mysql"],
    "Permission_Denied":  ["chmod", "chown", "ls", "stat", "id"],
    "Auth_Error":         ["systemctl", "journalctl", "cat", "grep"],
    "Memory_Leak":        ["pkill", "kill", "top", "free", "ps"],
    "Configuration_Error":["cat", "grep", "journalctl", "systemctl"],
}

KNOWN_CMDS = {
    "pkill","kill","top","free","vmstat","df","du","rm","find","truncate",
    "ping","ss","netstat","curl","traceroute","lsof","systemctl","journalctl",
    "chmod","chown","ls","stat","id","cat","grep","ps","ulimit","restart",
    "service","nginx","python3","python","cp","mv","ln","mkdir","echo",
}

PROSE_STARTERS = {
    "to","in","please","you","first","the","this","here","note",
    "i","we","it","if","use","run","try","make","sure","for",
}

# ── 프롬프트 빌더 3종 ────────────────────────────────────────────────────
_EXAMPLES_4 = """\
Error: nginx bind() to 0.0.0.0:80 failed
Command: systemctl restart nginx

Error: CUDA out of memory
Command: pkill -f python

Error: no space left on device
Command: df -h

Error: too many open files
Command: ulimit -n 65536"""

_EXAMPLES_2 = """\
Error: nginx bind() to 0.0.0.0:80 failed
Command: systemctl restart nginx

Error: CUDA out of memory
Command: pkill -f python"""

_HEADER = "You are a Self-Healing MLOps Agent. Reply with ONE raw Linux command only. No markdown, no backticks, no explanation, no sudo.\n\n"


def _build_prompt_a(error_log: str, system_ctx: str) -> str:
    return (
        _HEADER
        + _EXAMPLES_4
        + f"\n\nSystem: {system_ctx}\nError: {error_log}\nCommand:"
    )


def _build_prompt_b(error_log: str, system_ctx: str) -> str:
    return (
        _HEADER
        + f"System: {system_ctx}\n\n"
        + _EXAMPLES_4
        + f"\n\nError: {error_log}\nCommand:"
    )


def _build_prompt_c(error_log: str, _system_ctx: str) -> str:
    return (
        _HEADER
        + _EXAMPLES_2
        + f"\n\nError: {error_log}\nCommand:"
    )


PROMPT_VARIANTS: dict[str, callable] = {
    "A_current":         _build_prompt_a,
    "B_system_first":    _build_prompt_b,
    "C_few_shot_2":      _build_prompt_c,
}

# ── 출력 정제 (llm_engine._clean_llm_output 동일 로직) ──────────────────
def _clean(raw: str) -> str:
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("```") or line.startswith("#"):
            continue
        line = line.removeprefix("bash").strip()
        if line.startswith("sudo "):
            line = line[5:].strip()
        first = line.split()[0] if line.split() else ""
        if first.startswith("**") or first.rstrip(".,:").isdigit():
            continue
        if first.lower().rstrip(".,:") in PROSE_STARTERS:
            continue
        return line
    return ""


# ── Ollama 호출 ──────────────────────────────────────────────────────────
def _call_ollama(prompt: str) -> tuple[str, float]:
    """(cleaned_command, latency_ms) 반환. 실패 시 ("ERROR:...", latency)."""
    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0, "num_predict": 24},
    }).encode()

    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result  = json.loads(resp.read())
            command = _clean(result.get("response", ""))
            latency = (time.perf_counter() - t0) * 1000
            return (command or "ERROR: empty", latency)
    except Exception as e:
        return (f"ERROR: {e}", (time.perf_counter() - t0) * 1000)


# ── 품질 판정 ────────────────────────────────────────────────────────────
def _format_ok(cmd: str) -> bool:
    if cmd.startswith("ERROR:") or not cmd:
        return False
    if "\n" in cmd or "```" in cmd:
        return False
    if cmd.startswith("sudo "):
        return False
    return True


def _cmd_valid(cmd: str) -> bool:
    if not _format_ok(cmd):
        return False
    first = cmd.split()[0].lower()
    return first in KNOWN_CMDS


def _aligned(cmd: str, category: str) -> bool:
    hints = CATEGORY_CMD_HINTS.get(category, [])
    if not hints:
        return _cmd_valid(cmd)
    first = cmd.split()[0].lower() if cmd and not cmd.startswith("ERROR:") else ""
    return first in hints


# ── 메인 ────────────────────────────────────────────────────────────────
def main():
    # Ollama 체크
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
    except Exception:
        print("ERROR: Ollama 미실행 — 스크립트만 작성, 실행 스킵")
        sys.exit(0)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_samples = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))["data"]
    sample_size = len(all_samples)

    # 카테고리 균형 샘플링 (전체 test_set 사용)
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for s in all_samples:
        buckets[s["error_category"]].append(s)
    samples: list[dict] = []
    per_cat = max(1, sample_size // len(buckets))
    for cat_samples in buckets.values():
        samples.extend(cat_samples[:per_cat])
    samples = samples[:sample_size]

    system_ctx = "CPU: 45% | MEM: 6.2GB/15.6GB | DISK: 78% | LOAD: 1.2"

    rows     = []
    summary  = {v: {"format_ok": 0, "cmd_valid": 0, "aligned": 0, "latencies": []}
                for v in PROMPT_VARIANTS}

    total = len(samples) * len(PROMPT_VARIANTS)
    done  = 0

    for sample in samples:
        log_text  = sample["log_text"][:MAX_LOG_CHARS]
        category  = sample["error_category"]

        for variant_name, builder in PROMPT_VARIANTS.items():
            prompt = builder(log_text, system_ctx)
            cmd, lat = _call_ollama(prompt)

            fmt = _format_ok(cmd)
            val = _cmd_valid(cmd)
            aln = _aligned(cmd, category)

            rows.append({
                "variant":      variant_name,
                "category":     category,
                "command":      cmd,
                "format_ok":    int(fmt),
                "cmd_valid":    int(val),
                "aligned":      int(aln),
                "latency_ms":   round(lat, 1),
            })
            s = summary[variant_name]
            s["format_ok"] += int(fmt)
            s["cmd_valid"]  += int(val)
            s["aligned"]    += int(aln)
            s["latencies"].append(lat)

            done += 1
            print(f"[{done}/{total}] {variant_name} | {category[:20]:<20} | cmd={cmd[:30]:<30} | lat={lat:.0f}ms")

    # ── 결과 CSV ─────────────────────────────────────────────────────────
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"prompt_ab_results_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    # ── 집계 출력 ────────────────────────────────────────────────────────
    import math
    n = len(samples)
    print(f"\n{'Variant':<20} {'Format%':>8} {'Valid%':>8} {'Aligned%':>9} {'Avg(ms)':>8} {'P95(ms)':>8} {'Std(ms)':>8}")
    print("-" * 75)
    for vname, st in summary.items():
        lats    = sorted(st["latencies"])
        avg_lat = sum(lats) / len(lats)
        p95_lat = lats[int(len(lats) * 0.95)]
        std_lat = math.sqrt(sum((x - avg_lat) ** 2 for x in lats) / len(lats))
        print(
            f"{vname:<20} "
            f"{st['format_ok']/n*100:>7.1f}% "
            f"{st['cmd_valid']/n*100:>7.1f}% "
            f"{st['aligned']/n*100:>8.1f}% "
            f"{avg_lat:>7.1f} "
            f"{p95_lat:>7.1f} "
            f"{std_lat:>7.1f}"
        )

    print(f"\nCSV 저장: {csv_path}")
    print(f"샘플 수: {n}개 × {len(PROMPT_VARIANTS)}가지 = {total}건")


if __name__ == "__main__":
    main()
