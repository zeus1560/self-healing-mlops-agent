"""
RAGEngine — L1 Cache(ChromaDB) + L2 Fallback(Groq → Ollama → ipex_llm → Rule) 투트랙 추론 엔진.

아키텍처:
  analyze_error(log) 호출 시:
    1. ChromaDB 벡터 검색 (L1 — 임계치 이하 거리)
       → 적중 시 앙상블 투표로 최적 메타데이터 선택 → AgentResponse 즉시 반환
    2. L1 미스 → _l2_slow_track()
       a. Groq API (llama-3.3-70b, GROQ_API_KEY 설정 시 1순위)
       b. Ollama (경량 LLM, keep_alive로 메모리 상주 — Groq 미설정/실패 시 폴백)
       c. ipex_llm (spawn 프로세스, VRAM 반환 보장)
       d. Rule-based heuristic
       e. 인간 에스컬레이션

ChromaDB 싱글톤:
  Double-Checked Locking으로 프로세스 내 클라이언트를 하나만 유지.
  파일 락 경합 및 중복 연결 방지.
  초기화 실패 시 _chroma_client를 None으로 유지해 다음 호출에서 재시도 가능.

ipex_llm 메모리 설계:
  spawn 방식으로 격리된 자식 프로세스에서 모델을 로드하고,
  추론 완료 후 os._exit(0)으로 즉시 종료해 VRAM을 즉시 반환한다.
  parent_conn은 finally 블록에서 반드시 닫힌다.

디렉터리 경로:
  CHROMA_PERSIST_DIR 환경 변수로 재정의 가능.
  미설정 시 __file__ 기준 2레벨 상위의 data/chroma_db를 사용.
  os.getcwd() 의존을 제거해 실행 디렉터리 변경에 독립적이다.
"""
import hashlib
import json
import logging
import multiprocessing as mp
import os
import re as _re
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

from src.system_diagnostics import gather_system_context
from src.schemas import AgentResponse, ActionType

# log_watcher.py는 llm_engine을 slack_bot/telegram_bot보다 먼저 임포트하므로,
# 모듈 레벨 os.getenv() 호출 전에 이 자리에서 직접 .env를 로드해야 한다.
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
# llama-3.3-70b-versatile는 Groq에서 단종됨 (2026-08 기준).
# qwen/qwen3.x-27b + reasoning_effort=none 조합이 사고형(thinking) 오버헤드 없이
# 안정적으로 원샷 명령어를 반환하는 것으로 검증됨 — gpt-oss 계열은 harmony 포맷상
# reasoning 채널을 강제로 소비해 max_tokens 내에서 content가 비는 문제가 있었음.
# 2026-09-15 발견: qwen/qwen3.6-27b도 이 사이 Groq에서 단종되어(위 llama와 동일
# 패턴 반복) VM의 L2 경로 전체가 조용히 404로 실패하고 있었다 — Groq 모델은
# 언제든 예고 없이 단종될 수 있다는 뜻이므로, 앞으로 이 상수를 참조할 땐 실제
# 가용 모델인지(https://api.groq.com/openai/v1/models) 먼저 확인할 것.
GROQ_MODEL          = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_API_URL        = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
_GROQ_MAX_RETRIES   = int(os.getenv("GROQ_MAX_RETRIES", "2"))
_GROQ_RETRY_BASE    = float(os.getenv("GROQ_RETRY_BASE_SEC", "2.0"))

OLLAMA_BASE_URL     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL        = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
_OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "3"))
_OLLAMA_RETRY_BASE  = float(os.getenv("OLLAMA_RETRY_BASE_SEC", "2.0"))
_OLLAMA_KEEP_ALIVE  = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
# L1 캐시 적중 판별 거리 임계치 — 낮을수록 엄격
_RAG_THRESHOLD      = float(os.getenv("RAG_THRESHOLD", "0.6"))

# 온라인학습(learn_from_feedback) 엔트리 품질관리 — 반복 실패 시 자동 제거하는 기준.
# 실패 1건으로 바로 없애지 않고(우연한 실패 방어), 최소 실패 건수 + 실패율 둘 다 넘어야 삭제한다.
_LEARNED_ENTRY_MAX_FAILURES = int(os.getenv("LEARNED_ENTRY_MAX_FAILURES", "2"))
_LEARNED_ENTRY_FAILURE_RATE_THRESHOLD = float(
    os.getenv("LEARNED_ENTRY_FAILURE_RATE_THRESHOLD", "0.5")
)

# ChromaDB 퍼시스턴트 디렉터리.
# __file__ 기준 경로를 사용해 os.getcwd() 변경에 독립적으로 동작한다.
CHROMA_PERSIST_DIR = os.getenv(
    "CHROMA_PERSIST_DIR",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "chroma_db",
    ),
)

# ── 정규식 프리컴파일 ──────────────────────────────────────────────────────────
# 모듈 임포트 시 1회만 컴파일해 _clean_llm_output() 반복 호출 비용을 절감한다.
_JSON_PATTERN   = _re.compile(r'\{[^{}]*\}', _re.DOTALL)
_RE_CODE_BLOCK  = _re.compile(r'```[a-z]*\n(.*?)```', _re.DOTALL)
_RE_SHELL_TAG   = _re.compile(r'^(bash|sh|shell|zsh)\s+', _re.IGNORECASE)
_RE_LIST_PREFIX = _re.compile(r'^(\d+[\.\)]\s*|[-*]\s+)')
_RE_INLINE_CMT  = _re.compile(r'\s+#\s+.*$')

_PROSE_STARTERS = {
    "to", "in", "on", "at", "by", "of", "an", "a",
    "i", "we", "it", "if", "you",
    "please", "use", "run", "try", "make", "sure", "check",
    "first", "next", "then", "finally", "now", "step",
    "the", "this", "that", "these", "those", "here", "note",
    "as", "so", "since", "based", "given", "when", "where",
    "however", "therefore", "because", "also", "simply",
}

# ── ChromaDB Singleton ────────────────────────────────────────────────────────
# Double-Checked Locking: 1차 검사(락 없이)는 초기화 완료 후 빠른 경로.
# 초기화 실패 시 _chroma_client = None 이 유지되어 다음 호출에서 재시도된다.
_chroma_client = None
_chroma_lock   = threading.Lock()


def _get_chroma_client() -> chromadb.PersistentClient:
    """
    프로세스 내 ChromaDB 싱글톤 클라이언트를 반환한다.

    CHROMA_PERSIST_DIR 디렉터리가 없으면 자동 생성한다.
    초기화 실패 시 예외를 로깅하고 그대로 전파한다.
    (_chroma_client 는 None으로 유지되어 다음 호출에서 재시도 가능)
    """
    global _chroma_client
    if _chroma_client is None:
        with _chroma_lock:
            if _chroma_client is None:
                os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)
                try:
                    _chroma_client = chromadb.PersistentClient(
                        path=CHROMA_PERSIST_DIR,
                        settings=Settings(anonymized_telemetry=False),
                    )
                    logging.info(
                        f"[ChromaDB] Singleton 클라이언트 초기화 완료. "
                        f"(경로: {CHROMA_PERSIST_DIR})"
                    )
                except Exception:
                    # _chroma_client 를 None으로 유지 → 다음 호출에서 재시도
                    logging.error(
                        f"[ChromaDB] 초기화 실패 — 다음 호출에서 재시도됩니다:\n"
                        f"{traceback.format_exc()}"
                    )
                    raise
    return _chroma_client


# ── Rule-based Heuristic Fallback ─────────────────────────────────────────────
_ERROR_RULES: list[tuple[tuple[str, ...], str]] = [
    (("out of memory", "oom killer", "cannot allocate memory",
      "cuda out of memory", "vram"), "pkill -f python"),
    (("no space left on device", "disk full", "disk space"), "df -h"),
    (("address already in use", "bind() failed", "port 80", "port 443"),
     "systemctl restart nginx"),
    (("nginx",),      "systemctl restart nginx"),
    (("postgresql", "postgres"), "systemctl restart postgresql"),
    (("too many open files",), "ulimit -n 65536"),
    (("connection refused", "connection timeout"), "ss -tuln"),
]


def _rule_based_fallback(error_log: str) -> tuple[str, str] | None:
    """
    에러 로그 키워드로 규칙 기반 명령어를 찾는다. 미매칭 시 None.

    반환: (command, matched_keywords_desc) — 매칭된 키워드 자체를 같이 반환해
    호출부가 reasoning에 "어떤 키워드로 왜 이 명령을 골랐는지"를 구체적으로
    담을 수 있게 한다(예전엔 "규칙 기반 키워드 매칭"이라는 고정 문구만 나가서
    실제 매칭 근거가 안 보이는 블랙박스였다 — 2026-09-17 코드 신뢰도 점검에서 발견).
    """
    lower = error_log.lower()
    for keywords, command in _ERROR_RULES:
        matched = [kw for kw in keywords if kw in lower]
        if matched:
            return command, ", ".join(f"'{kw}'" for kw in matched)
    return None


# ── Prompt Helpers ────────────────────────────────────────────────────────────
def _build_prompt(error_log: str, system_context: str) -> str:
    """Few-shot 예시 포함 명령어 추론 프롬프트를 생성한다."""
    return (
        "You are a Self-Healing MLOps Agent. "
        "Reply with ONE raw Linux command only. "
        "No markdown, no backticks, no explanation, no sudo.\n\n"
        "Error: nginx bind() to 0.0.0.0:80 failed\n"
        "Command: systemctl restart nginx\n\n"
        "Error: CUDA out of memory\n"
        "Command: pkill -f python\n\n"
        "Error: no space left on device\n"
        "Command: df -h\n\n"
        "Error: too many open files\n"
        "Command: ulimit -n 65536\n\n"
        f"System: {system_context}\n"
        f"Error: {error_log}\n"
        "Command:"
    )


def _extract_json_command(text: str) -> str:
    """
    LLM 응답에서 {"command": "..."} 형태의 JSON을 추출한다.
    0.5B 모델이 지시를 어기고 JSON 형식으로 답변할 때 방어.
    """
    for m in _JSON_PATTERN.finditer(text):
        try:
            obj = json.loads(m.group())
            cmd = obj.get("command") or obj.get("cmd") or obj.get("action")
            if cmd and isinstance(cmd, str):
                return cmd.strip()
        except (json.JSONDecodeError, AttributeError):
            continue
    return ""


def _clean_llm_output(raw: str) -> str:
    """
    LLM 응답에서 첫 번째 유효한 셸 명령어만 추출한다.

    처리 순서:
      1. JSON 형태 응답 → _extract_json_command
      2. 마크다운 코드블록 → 블록 안 첫 줄
      3. 줄 단위 산문 필터 → 첫 번째 비산문 줄
      4. 인라인 주석(#) 제거, sudo 제거

    모든 정규식은 모듈 레벨에서 프리컴파일(_RE_*)되어 반복 호출 비용을 최소화한다.
    """
    if not raw:
        return ""

    # 1. JSON 형태로 답한 경우
    if "{" in raw:
        extracted = _extract_json_command(raw)
        if extracted:
            raw = extracted

    # 2. 마크다운 코드블록 내부 추출
    code_block = _RE_CODE_BLOCK.search(raw)
    if code_block:
        raw = code_block.group(1)

    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("```") or line.startswith("===") or line.startswith("---"):
            continue
        line = _RE_SHELL_TAG.sub('', line)
        if line.startswith("sudo "):
            line = line[5:].strip()
        line = _RE_LIST_PREFIX.sub('', line)
        if not line:
            continue
        line = _RE_INLINE_CMT.sub('', line).strip()

        tokens = line.split()
        if not tokens:
            continue
        first = tokens[0].lower().rstrip(".,:;")
        if first.startswith("**") or first.isdigit():
            continue
        if first in _PROSE_STARTERS:
            continue
        return line

    return ""


# ── Ollama ────────────────────────────────────────────────────────────────────
def _ollama_warmup() -> None:
    """
    에이전트 시작 시 백그라운드에서 Ollama 모델을 미리 로드한다.
    빈 프롬프트로 generate 요청 → 토큰 생성 없이 모델만 메모리에 올림.
    RAGEngine 생성자에서 1회 호출되며, 싱글톤 구조로 중복 호출되지 않는다.
    """
    def _load():
        try:
            payload = json.dumps({
                "model":      OLLAMA_MODEL,
                "prompt":     "",
                "keep_alive": _OLLAMA_KEEP_ALIVE,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{OLLAMA_BASE_URL}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60):
                pass
            logging.info(
                f"[Ollama Warmup] 모델 '{OLLAMA_MODEL}' 사전 로딩 완료 "
                f"(keep_alive={_OLLAMA_KEEP_ALIVE})."
            )
        except Exception:
            logging.warning(
                f"[Ollama Warmup] 사전 로딩 실패 (Ollama 미실행 시 정상):\n"
                f"{traceback.format_exc()}"
            )

    threading.Thread(target=_load, daemon=True, name="ollama-warmup").start()


def _is_ollama_available() -> bool:
    """헬스체크 2회 시도 — 순단(transient failure)으로 인한 오탐 방지."""
    for attempt in range(2):
        try:
            urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
            return True
        except Exception as e:
            logging.debug(f"[Ollama] 헬스체크 실패 ({attempt + 1}/2): {e}")
    return False


def _run_ollama(error_log: str, system_context: str, timeout: int = 60) -> str:
    """Ollama API를 호출해 명령어를 추론한다. 실패 시 'ERROR:...' 반환."""
    prompt  = _build_prompt(error_log, system_context)
    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0, "num_predict": 24},
    }).encode()

    last_error = ""
    for attempt in range(1, _OLLAMA_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                f"{OLLAMA_BASE_URL}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result  = json.loads(resp.read())
                raw     = result.get("response", "")
                command = _clean_llm_output(raw)
                if not command:
                    logging.warning(
                        f"[Ollama] 유효 명령어 추출 실패. 원본 응답: {raw!r:.120}"
                    )
                    return "ERROR: Ollama returned unparseable response"
                if attempt > 1:
                    logging.info(f"[Ollama] {attempt}번째 시도에서 성공")
                return command

        except urllib.error.URLError as e:
            last_error = str(e)
            if attempt < _OLLAMA_MAX_RETRIES:
                wait = _OLLAMA_RETRY_BASE ** attempt  # 2s, 4s ...
                logging.warning(
                    f"[Ollama] 연결 실패 ({attempt}/{_OLLAMA_MAX_RETRIES}), "
                    f"{wait:.0f}초 후 재시도: {e}"
                )
                time.sleep(wait)
        except Exception as e:
            return f"ERROR: Ollama call failed - {e}"

    return f"ERROR: Ollama not reachable after {_OLLAMA_MAX_RETRIES} attempts - {last_error}"


# ── Groq ──────────────────────────────────────────────────────────────────────
def _is_groq_available() -> bool:
    """GROQ_API_KEY 설정 여부로 판별한다 (별도 헬스체크 엔드포인트 없음)."""
    return bool(GROQ_API_KEY)


def _call_groq_chat(prompt: str, max_tokens: int, timeout: int) -> str:
    """
    Groq Chat Completions API(OpenAI 호환)를 호출해 원본 응답 텍스트를 반환한다.
    실패 시 'ERROR:...' 문자열을 반환한다 (예외를 던지지 않음 — 호출측 폴백 체인 유지).
    """
    payload = json.dumps({
        "model":            GROQ_MODEL,
        "messages":         [{"role": "user", "content": prompt}],
        "temperature":      0,
        "max_tokens":       max_tokens,
        # qwen3 계열의 <think> 체인 생성을 비활성화해 max_tokens 내 응답 유실을 방지한다.
        # 미지원 모델(예: gpt-oss)로 GROQ_MODEL이 바뀌면 Groq가 무시하거나 400을 반환할 수 있음.
        "reasoning_effort": "none",
    }).encode()

    last_error = ""
    for attempt in range(1, _GROQ_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                GROQ_API_URL,
                data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    # 기본 urllib UA("Python-urllib/x.x")는 Cloudflare에 차단(1010)되므로 명시 지정.
                    "User-Agent":    "self-healing-mlops-agent/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                return result["choices"][0]["message"]["content"]

        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:200]
            last_error = f"HTTP {e.code}: {body}"
            if e.code == 429 and attempt < _GROQ_MAX_RETRIES:
                wait = _GROQ_RETRY_BASE ** attempt
                logging.warning(f"[Groq] Rate limit(429), {wait:.0f}초 후 재시도: {last_error}")
                time.sleep(wait)
                continue
            break
        except urllib.error.URLError as e:
            last_error = str(e)
            if attempt < _GROQ_MAX_RETRIES:
                wait = _GROQ_RETRY_BASE ** attempt
                logging.warning(
                    f"[Groq] 연결 실패 ({attempt}/{_GROQ_MAX_RETRIES}), {wait:.0f}초 후 재시도: {e}"
                )
                time.sleep(wait)
        except Exception as e:
            return f"ERROR: Groq call failed - {e}"

    return f"ERROR: Groq not reachable - {last_error}"


def _run_groq(error_log: str, system_context: str, timeout: int = 30) -> str:
    """Groq API를 호출해 명령어를 추론한다. 실패 시 'ERROR:...' 반환."""
    prompt = _build_prompt(error_log, system_context)
    raw    = _call_groq_chat(prompt, max_tokens=24, timeout=timeout)
    if raw.startswith("ERROR:"):
        return raw

    command = _clean_llm_output(raw)
    if not command:
        logging.warning(f"[Groq] 유효 명령어 추출 실패. 원본 응답: {raw!r:.120}")
        return "ERROR: Groq returned unparseable response"
    return command


# DOTALL: 프롬프트가 "한 줄로" 답하라고 요청하지만 LLM이 항상 지키진 않는다
# (max_tokens=60 아래서 잘리며 줄바꿈이 낄 수 있음) — 2026-09-15 code-review
# 발견. DOTALL 없이는 root_cause/target에 개행이 섞이자마자 "."이 그 줄바꿈을
# 못 건너뛰어 매치 자체가 실패해, 파싱 가능한 내용도 조용히 버려지고 있었다.
_DIAGNOSIS_RE = _re.compile(
    r"ROOT_CAUSE:\s*(?P<root_cause>.*?)\s*\|\s*ACTION_TYPE:\s*(?P<action_type>\S+)\s*\|\s*"
    r"TARGET:\s*(?P<target>.*)",
    _re.IGNORECASE | _re.DOTALL,
)


def _parse_diagnosis(raw: str) -> dict | None:
    """진단 에이전트 응답("ROOT_CAUSE: ... | ACTION_TYPE: ... | TARGET: ...")을 파싱한다.

    형식이 안 맞으면(모델이 지시를 무시한 경우) None을 반환해 호출부가 진단 없이
    기존 방식(단일 프롬프트 생성)으로 안전하게 폴백하게 한다.
    """
    m = _DIAGNOSIS_RE.search(raw.strip())
    if not m:
        return None
    return {
        "root_cause":  m.group("root_cause").strip(),
        "action_type": m.group("action_type").strip().lower(),
        "target":      m.group("target").strip(),
    }


def _diagnose_error(error_log: str, system_context: str) -> dict | None:
    """
    멀티에이전트 3단계(진단→제안→검토) 중 1단계 — 명령을 생성하기 전에 원인·
    권장 조치 유형·대상을 먼저 구조화해 뽑아낸다(2026-09-15 추가).

    기존엔 _run_groq() 하나가 "원인 파악"과 "명령 생성"을 한 프롬프트에서 동시에
    했다 — 2026-09-15 faithfulness 실측에서 프롬프트가 여러 판단(허용 명령 여부 +
    타겟 적절성)을 뒤섞을 때 엉뚱한 결론(예: 화이트리스트에 있는 fuser를 "없다"고
    오판)이 나오는 걸 확인했는데, 같은 위험이 명령 생성 단계에도 있을 수 있다는
    문제의식에서 판단을 분리했다.

    실패(네트워크 오류, 파싱 실패, GROQ_API_KEY 미설정)하면 None을 반환한다 —
    이 함수는 어디까지나 기존 생성 프롬프트를 보강하는 참고 정보이므로, 실패해도
    호출부(_l2_slow_track)가 진단 없이 기존 방식 그대로 진행하게 하며 새로운
    실패 모드를 추가하지 않는다. Ollama/ipex_llm 폴백 경로는 이 진단 단계를
    거치지 않는다(실사용 빈도가 낮은 하위 폴백까지 복잡도를 늘릴 필요는 없다고
    판단, Groq가 사실상 유일하게 상시 가동되는 L2 백엔드).
    """
    if not _is_groq_available():
        return None
    prompt = (
        "You are a diagnostic triage agent for a Self-Healing MLOps Agent.\n"
        "Given the error and system context below, identify: the likely root cause,\n"
        "the general category of fix needed, and (if applicable) the specific target\n"
        "(PID, service/process name, or port) that a fix should act on.\n"
        "Reply in EXACTLY this one-line, pipe-separated format:\n"
        "ROOT_CAUSE: <short phrase> | ACTION_TYPE: <restart_service|kill_process|"
        "clear_memory|other> | TARGET: <pid/name/port or 'none'>\n\n"
        f"Error: {error_log[:200]}\n"
        f"System: {system_context}\n"
    )
    raw = _call_groq_chat(prompt, max_tokens=60, timeout=15)
    if raw.startswith("ERROR:"):
        logging.warning(f"[진단 에이전트] Groq 호출 실패, 진단 생략: {raw}")
        return None
    diagnosis = _parse_diagnosis(raw)
    if diagnosis is None:
        logging.warning(f"[진단 에이전트] 응답 형식 불일치, 진단 생략: {raw!r:.150}")
    return diagnosis


# 진단 에이전트의 action_type → L1과 동일한 구조화 ActionType 매핑. "other"
# (또는 이 매핑에 없는 값)는 자유형식 명령 생성 경로로 그대로 폴백한다.
_STRUCTURED_ACTION_TYPES: dict[str, ActionType] = {
    "restart_service": ActionType.RESTART_SERVICE,
    "kill_process":     ActionType.KILL_PROCESS,
    "clear_memory":     ActionType.CLEAR_MEMORY,
}


def _route_from_diagnosis(
    diagnosis: dict, diagnosis_summary: str,
    nearest_category: str | None, best_distance: float,
) -> AgentResponse | None:
    """
    진단 에이전트가 구조화된 조치를 확신 있게 제안하면, 자유형식 명령 생성
    (_run_groq)과 self-reflection 검토를 건너뛰고 L1과 동일한 구조화 액션
    경로로 바로 연결한다(2026-09-17 — 이전엔 diagnosis가 action_type/target을
    뽑아도 _run_groq 프롬프트를 보강하는 텍스트 힌트로만 쓰이고
    AgentResponse.action_type 결정엔 전혀 반영되지 않았다).

    라우팅 불가 시(아래 조건들) None을 반환해 호출부가 기존 자유형식
    경로(제안+self-reflection 검토)로 그대로 진행하게 한다 — 새로운 실패
    모드를 추가하지 않는다는 _diagnose_error와 같은 원칙.

    **PID로 보이는 대상은 구조화 라우팅하지 않는다**: executor.py의
    `_kill_process()`/`_restart_service()`는 `pkill -x <이름>`/`systemctl
    restart <이름>`처럼 정확한 프로세스/서비스 *이름* 일치만 지원하고 PID나
    포트 번호는 지원하지 않는데, 이 시스템의 실제 kill 대상은 대부분 에러
    로그에 찍힌 PID다(예: "leaky_worker.py (pid=5821)", 진단 에이전트는
    이를 "pid 5821"처럼 자유형식으로 낸다 — 순수 숫자만이 아니다). target에
    숫자가 하나라도 있으면 PID/포트로 보고 구조화 라우팅을 포기, 기존
    자유형식 경로(`kill -TERM <pid>` 생성 + self-reflection 검토)로
    폴백한다. 이 조건이 없으면 두 갈래로 조용히 틀렸다: "5821"(순수 숫자)은
    `_validate_process_name`의 문자 정규식을 통과해버려 존재하지도 않을
    "5821"이라는 이름의 프로세스를 찾다가 조용히 실패하고, "pid 5821"(공백
    포함)은 그 정규식 자체에 걸려 SecurityBlock으로 실패한다 — 둘 다 기존
    자유형식 경로라면 정상 처리됐을 요청을 헛되이 태워버리는 회귀다.

    **설명문·경로처럼 보이는 대상도 마찬가지다** (2026-09-22, §3.1/§6에서
    16건 중 4건꼴로 재현된 버그 수정): 진단 에이전트가 TARGET에 깔끔한
    프로세스/서비스 이름 대신 "mlflow tracking server"(공백 섞인 설명문)나
    "/tmp/tensorboard_logs"(파일 경로)를 낼 때가 있다. 둘 다
    `_validate_process_name`의 `_PROCESS_NAME_RE`(영숫자·`_-.`만 허용)에
    걸려 SecurityBlock으로 무조건 실패하는데, 이걸 구조화 라우팅으로
    넘기면 자유형식 경로라면 정상 처리됐을(또는 최소한 self-reflection이
    걸러줬을) 요청을 똑같이 헛되이 태운다. PID/포트와 같은 이유로, target에
    공백이나 `/`가 하나라도 있으면 깔끔한 식별자가 아니라고 보고 구조화
    라우팅을 포기한다.

    **self-reflection을 건너뛰는 게 새 위험은 아니다**: L1이 이미 이 세
    ActionType을 self-reflection 없이 실행해왔고, 여기서도 동일한
    `_validate_process_name()` 검증 + autonomy 게이트(approve_then_execute
    승인 또는 auto 즉시실행)를 그대로 탄다. self-reflection이 지키는 "타겟이
    실제 에러와 맞는가"는 대상이 PID/패턴처럼 애매한 자유형식 경로에서 더
    필요한 검증이라, 그 경로는 그대로 유지한다.
    """
    action_type = diagnosis["action_type"]
    if action_type not in _STRUCTURED_ACTION_TYPES:
        return None  # "other" 등 — 기존 자유형식 경로로 폴백

    target = diagnosis["target"].strip()
    has_target = bool(target) and target.lower() != "none"
    looks_like_pid_or_port = bool(_re.search(r"\d", target))
    looks_like_description_or_path = bool(_re.search(r"[\s/]", target))

    if action_type in ("restart_service", "kill_process") and (
        not has_target or looks_like_pid_or_port or looks_like_description_or_path
    ):
        return None

    action_enum = _STRUCTURED_ACTION_TYPES[action_type]
    logging.info(
        f"[진단 에이전트] 구조화 라우팅 → {action_enum.value}"
        + (f" (대상: {target})" if has_target else "")
    )
    return AgentResponse(
        error_category="LLM_Inferred", severity="HIGH",
        action_type=action_enum,
        target_process=target if has_target else None,
        reasoning=f"진단 에이전트 구조화 라우팅 — {diagnosis['root_cause']}",
        resolution_source="L2_LLM",
        l1_nearest_category=nearest_category,
        l1_nearest_distance=best_distance,
        l2_diagnosis=diagnosis_summary,
    )


_READ_ONLY_COMMANDS = frozenset({"df", "free", "ps", "ss", "netstat", "uptime", "echo"})


def _is_read_only_command(command: str) -> bool:
    """
    부작용 없는 조회성 명령어인지 판별한다 (systemctl status 포함).

    executor.py의 ALLOWED_COMMANDS 화이트리스트 안에서도 이 부분집합은
    시스템 상태를 절대 바꾸지 않으므로, LLM 판정 없이 항상 안전으로 취급한다.
    2026-09-05 발견: Groq 온도=0 호출도 'systemctl status postgresql' 같은
    완전히 무해한 조회 명령을 매번 다른 판정(YES/NO 뒤섞임)으로 거부한 사례가
    있어, 조회성 명령은 LLM 판정 자체를 아예 건너뛰도록 결정론적으로 처리한다.
    """
    tokens = command.split()
    if not tokens:
        return False
    if tokens[0] in _READ_ONLY_COMMANDS:
        return True
    if tokens[0] == "systemctl" and len(tokens) >= 2 and tokens[1] == "status":
        return True
    return False


# systemctl(restart/start/stop, status는 이미 _is_read_only_command에서 처리)/
# nginx(-s reload·test만)/journalctl(--vacuum-*만)/ulimit(세션 범위, 타 프로세스
# 영향 없음)는 executor.py 화이트리스트에서 인자·서비스 이름까지 정규식으로
# 좁게 검증되므로 "위험한가"는 이미 구조적으로 답이 나와있다. LLM 판정이 실제로
# 의미 있는 건 kill/pkill/fuser처럼 대상(PID·패턴)을 잘못 지정할 위험이 남는
# 명령뿐 — 2026-09-05에 확인된 노이즈('systemctl restart postgresql' 온도=0에도
# YES/NO 뒤섞임)는 전부 이 부류였다.
_BOUNDED_STATE_CHANGE_ARGS: dict[str, frozenset[str] | None] = {
    "systemctl":  frozenset({"restart", "start", "stop"}),
    "nginx":      frozenset({"-s", "reload", "test"}),
    "journalctl": None,
    "ulimit":     None,
}


def _is_bounded_state_change_command(command: str) -> bool:
    """
    부작용은 있지만 executor.py 화이트리스트가 이미 인자까지 좁혀 검증해서
    "위험한가"에 대한 답이 구조적으로 끝난 명령어인지 판별한다.
    """
    tokens = command.split()
    if not tokens:
        return False
    base = tokens[0]
    if base not in _BOUNDED_STATE_CHANGE_ARGS:
        return False
    allowed = _BOUNDED_STATE_CHANGE_ARGS[base]
    if allowed is None:
        return True
    return len(tokens) >= 2 and tokens[1] in allowed


def _is_self_destructive_kill(command: str) -> bool:
    """
    kill로 PID 1을 지정하는지 판별한다.

    컨테이너에선 PID 1이 target-app 자기 자신(uvicorn)이고, 실서버에선
    init/systemd다 — 어느 쪽이든 죽이면 안 되는 대상이라는 사실 자체로 이미
    답이 나와있는 구조적 위험이다. 2026-09-04 FP/FN 분석에서 실제로 관찰된
    사례(Groq가 'kill -9 1'을 제안)가 이 부류: executor.py 화이트리스트는
    -9(SIGKILL)는 막아주지만 -TERM/-HUP(허용된 소프트 신호)로 PID 1을
    지정하면 여전히 통과한다 — systemctl restart 노이즈와 마찬가지로
    LLM 판정에만 맡기면 매번 뒤섞이는 문제가 재현되므로, "PID 1"이라는
    사실만으로 LLM 호출 없이 항상 거부한다.

    2026-09-10 adversarial testing 중 발견: 문자열 "1" 정확 일치만 보면
    "001"/"+1"처럼 실제 kill(1) 유틸리티가 정수로 파싱해 똑같이 PID 1을
    지정하는 변형을 놓친다 — 정수로 파싱해 값을 비교한다.
    """
    tokens = command.split()
    if len(tokens) < 3 or tokens[0] != "kill":
        return False
    for t in tokens[2:]:
        try:
            if int(t) == 1:
                return True
        except ValueError:
            continue
    return False


_LEADING_NOISE_RE = _re.compile(r'^[^A-Za-z]*')


def _split_verdict_and_rationale(raw: str) -> tuple[bool, str]:
    """
    "YES: matches OOM recovery pattern" / "NO: targets unrelated service" 형태의
    응답을 (안전 여부, 근거)로 분리한다(Explainability, 2026-09-12 추가). 콜론이
    없으면(모델이 지시를 무시하고 YES/NO만 답한 경우) 라벨 자체를 근거로 남겨
    최소한의 정보라도 보존한다 — 완전히 빈 문자열보다 낫다.

    판정 앞의 마크다운/인용부호(예: "**YES**: ...")를 벗기지 않으면
    upper().startswith("YES")가 "**YES"에서 실패해 안전한 YES가 거부(NO)로
    뒤집히는 실제 파싱 버그가 있었음(실측으로 발견) — 문자로 시작할 때까지 선행
    비문자를 제거한 뒤 판정한다.
    """
    text  = raw.strip()
    verdict = _LEADING_NOISE_RE.sub("", text).upper()
    safe    = verdict.startswith("YES")
    rationale = text.split(":", 1)[1].strip() if ":" in text else text
    return safe, rationale


def _reflect_on_command(command: str, error_log: str, system_ctx: str) -> tuple[bool, str]:
    """
    자가 반성 루프 — LLM이 생성한 명령어의 안전성을 재검증한다.

    PID 1을 지정하는 kill(_is_self_destructive_kill)은 LLM 호출 없이 항상
    거부한다. 조회성 명령어(_is_read_only_command)와 화이트리스트가 이미
    좁혀 검증하는 상태변경 명령어(_is_bounded_state_change_command)는 LLM
    호출 없이 항상 통과시킨다. 나머지(kill/pkill/fuser 등 대상 지정 위험이
    남는 명령)는 Groq(GROQ_API_KEY 설정 시) 우선 사용, 실패/미설정 시
    Ollama로 폴백한다. YES → 실행 허용 / NO 또는 오류 → 에스컬레이션으로 전환.
    검증 실패(네트워크 오류 등) 시 보수적으로 True 반환한다.
    최종 방어선은 executor.py의 화이트리스트 검증이므로 이중 안전망이 유지된다.

    반환값은 (안전 여부, 판정 근거) — 2026-09-12 Explainability 확장으로 튜플이
    됐다(이전엔 bool만 반환). LLM 호출 없이 결정론적으로 통과/거부하는 3개 경로도
    각각 왜 그런지 이미 알고 있는 이유를 그대로 근거로 반환한다.

    2026-09-15 프롬프트 수정: run_faithfulness_test.py 실측(반사실적 위험요소
    조작 테스트)에서 실제 프로덕션 문제 발견 — 프롬프트의 허용 명령 설명에
    "fuser"가 아예 빠져있어 정확히 올바른 대상을 지정한 `fuser -k <port>`조차
    5/5 "화이트리스트에 없다"며 거부됐고, `pkill`도 3/5는 같은 이유로,
    2/5는 "명시적 소프트 시그널 플래그가 없다"(pkill의 실제 화이트리스트
    요건과 다름 — kill의 요건과 혼동)로 잘못 거부됨. 반면 대상이 실제로
    틀린 경우(엉뚱한 PID/포트/과도하게 넓은 패턴)는 87% 확률로 그 불일치를
    정확히 짚어내는 근거를 냄 — "타겟이 에러와 맞는지" 판단 자체는 신뢰할
    만했으나, 프롬프트가 fuser를 안 알려주고 pkill 요건을 애매하게 적어
    엉뚱한 이유로 안전한 액션까지 에스컬레이션시키고 있었다. fuser를
    명시하고 pkill/fuser엔 시그널 플래그가 불필요함을 명확히 해 수정.
    """
    if _is_self_destructive_kill(command):
        rationale = "PID 1(자기 자신/init) 대상 지정 — LLM 판정 없이 항상 거부"
        logging.info(f"[자가 반성] '{command}' — {rationale}")
        return False, rationale

    if _is_read_only_command(command):
        rationale = "조회성 명령어(상태 변경 없음) — 위험 없음, LLM 판정 생략"
        logging.info(f"[자가 반성] '{command}' — {rationale}")
        return True, rationale

    if _is_bounded_state_change_command(command):
        rationale = "화이트리스트로 인자·대상까지 이미 좁게 검증된 안전한 상태변경 명령 — LLM 판정 생략"
        logging.info(f"[자가 반성] '{command}' — {rationale}")
        return True, rationale

    prompt = (
        "You are a safety reviewer for a Self-Healing MLOps Agent.\n"
        "The proposed command has ALREADY passed a strict security whitelist — only a small\n"
        "fixed set of safe commands is even possible here: systemctl restart/stop/start/status,\n"
        "kill with soft signals only (-TERM/-HUP), pkill/fuser targeting a specific pattern or\n"
        "port (no explicit signal flag required — their default signal is already soft/safe),\n"
        "and memory/disk/process read-only queries. Shell chaining, arbitrary paths, and\n"
        "destructive tools are already blocked before this point.\n"
        "Do NOT reject a command merely because it looks unfamiliar or lacks an explicit signal\n"
        "flag — if it matches the categories above, it IS already whitelisted. Your job is only\n"
        "to judge whether the SPECIFIC TARGET (which PID/process name/port) is a reasonable,\n"
        "proportionate match for the described error.\n"
        "Reply with YES or NO, followed by a colon and a very short reason (max 15 words),\n"
        "e.g. 'YES: matches known OOM recovery pattern' or 'NO: targets unrelated service'.\n\n"
        f"Error: {error_log[:200]}\n"
        f"Proposed command: {command}\n"
        f"System: {system_ctx}\n\n"
        "Is this command a reasonable response to the error? (YES/NO: reason):"
    )

    if _is_groq_available():
        raw = _call_groq_chat(prompt, max_tokens=40, timeout=15)
        if not raw.startswith("ERROR:"):
            safe, rationale = _split_verdict_and_rationale(raw)
            logging.info(
                f"[자가 반성/Groq] 명령어='{command}' | 판정='{raw.strip()}' "
                f"→ {'통과' if safe else '거부'}"
            )
            return safe, rationale
        logging.warning(f"[자가 반성] Groq 검증 실패, Ollama로 폴백: {raw}")

    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0, "num_predict": 40},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read()).get("response", "").strip()
            safe, rationale = _split_verdict_and_rationale(raw)
            logging.info(
                f"[자가 반성/Ollama] 명령어='{command}' | 판정='{raw}' "
                f"→ {'통과' if safe else '거부'}"
            )
            return safe, rationale
    except Exception as e:
        logging.warning(f"[자가 반성] 검증 요청 실패 — 보수적 통과 처리: {e}")
        return True, f"자가 반성 검증 요청 실패(네트워크 오류 등) — 보수적으로 통과 처리: {e}"


# ── ipex_llm (Intel Arc GPU 환경 전용) ───────────────────────────────────────
mp_ctx = mp.get_context("spawn")


def _ipex_inference_worker(conn, error_log: str, system_context: str) -> None:
    """
    spawn 자식 프로세스에서 ipex_llm 모델을 로드하고 추론한다.

    os._exit(0)으로 즉시 종료해 VRAM을 즉시 반환한다.
    sys.exit은 atexit finalizer를 거치므로 VRAM 해제가 지연될 수 있어 사용하지 않는다.
    """
    import warnings
    warnings.filterwarnings("ignore")
    try:
        from ipex_llm.transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer

        hf_model_id = "Qwen/Qwen2.5-3B-Instruct"
        tokenizer   = AutoTokenizer.from_pretrained(hf_model_id)
        model       = AutoModelForCausalLM.from_pretrained(
            hf_model_id, load_in_4bit=True, optimize_model=True,
        ).to("cpu")

        prompt  = _build_prompt(error_log, system_context)
        encoded = tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
        outputs = model.generate(
            input_ids=encoded["input_ids"],
            attention_mask=encoded["attention_mask"],
            max_new_tokens=24,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
        generated = outputs[0][encoded["input_ids"].size(1):]
        command   = _clean_llm_output(
            tokenizer.decode(generated, skip_special_tokens=True)
        )
        conn.send({"status": "success", "result": command})
    except Exception as e:
        conn.send({"status": "error", "reason": f"{e}\n{traceback.format_exc()}"})
    finally:
        conn.close()
        os._exit(0)


def run_ipex_engine(error_log: str, system_context: str, timeout: int = 600) -> str:
    """
    ipex_llm 추론을 격리된 spawn 프로세스에서 실행하고 결과를 반환한다.

    parent_conn은 finally 블록에서 반드시 닫혀 파이프 파일 디스크립터 누수를 방지한다.
    OOM Killer 등으로 자식 프로세스가 강제 종료되면 EOFError를 포착해 명시적 오류를 반환한다.
    """
    parent_conn, child_conn = mp_ctx.Pipe()
    p = mp_ctx.Process(
        target=_ipex_inference_worker,
        args=(child_conn, error_log, system_context),
    )
    p.start()
    child_conn.close()  # 부모는 쓰기 끝을 닫아 EOF 감지가 정확하게 동작하게 한다

    try:
        if parent_conn.poll(timeout):
            # OOM Killer 등으로 자식이 강제 종료되면 파이프 쓰기 끝이 닫혀
            # poll()이 True를 반환하지만 recv()에서 EOFError가 발생한다.
            try:
                response = parent_conn.recv()
            except EOFError:
                logging.error(
                    "[ipex_llm] 자식 프로세스 비정상 종료 — 응답 없음 (EOFError). "
                    "OOM Killer에 의한 강제 종료일 가능성이 높습니다."
                )
                p.join()
                return "ERROR: ipex worker crashed without response (OOM or fatal signal)"
            p.join()
            return (
                response["result"]
                if response["status"] == "success"
                else f"ERROR: {response['reason']}"
            )
        else:
            logging.error("[ipex_llm] Timeout! 프로세스 강제 종료.")
            p.terminate()
            p.join()
            return "TIMEOUT"
    finally:
        # 예외·타임아웃 여부와 무관하게 파이프 파일 디스크립터를 반드시 닫는다.
        parent_conn.close()


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────
def _build_response_from_meta(
    meta: dict, source: str, doc_id: str | None = None, evidence: str | None = None
) -> AgentResponse:
    """ChromaDB 메타데이터 dict → AgentResponse. L1 fast track과 배치 쿼리가 공유.

    doc_id/meta의 "source" 필드는 L1_CACHE 히트에서 실제로 매칭된 문서를 다시
    식별하기 위함이다(온라인학습 엔트리의 실행 결과 되먹임에 사용, learn_from_feedback/
    record_learned_outcome 참고).
    evidence는 _ensemble_vote()가 만든 사람이 읽을 수 있는 투표 근거 설명(Explainability).
    """
    action_str = meta.get("action_type", "escalate_to_human")
    try:
        action_enum = ActionType(action_str)
    except ValueError:
        action_enum = ActionType.ESCALATE_TO_HUMAN
    return AgentResponse(
        error_category=meta.get("error_category", "Unknown"),
        severity="HIGH",
        action_type=action_enum,
        command=meta.get("command") or None,
        target_process=meta.get("target_process") or None,
        # 기본값을 "No reasoning found in DB" 같은 문구로 두면 대시보드/승인
        # 메시지가 이걸 진짜 self-reflection 판정으로 오인해 표시한다
        # (dashboard/app.py는 reasoning이 nan/None/""일 때만 "근거 없음"으로
        # 처리하는데, 이 placeholder 문자열은 그 체크에 안 걸렸다 — 2026-09-17
        # 실측 데모 중 발견). L1_CACHE 문서 중 curated 데이터(카오스 인젝터
        # 시그니처 등)는 애초에 "reasoning" 메타를 안 남기는 게 정상이므로
        # 빈 문자열로 둬서 l1_evidence만 근거로 노출되게 한다.
        reasoning=meta.get("reasoning") or "",
        resolution_source=source,
        l1_doc_id=doc_id,
        l1_source=meta.get("source"),
        l1_evidence=evidence,
    )


def _make_llm_response(
    command: str, error_log: str, system_context: str, backend: str,
    nearest_category: str | None = None, nearest_distance: float | None = None,
    diagnosis: str | None = None,
) -> AgentResponse:
    """
    자가 반성 결과를 반영해 EXECUTE_LLM_COMMAND AgentResponse를 만든다.

    2026-09-05 결정: 자가 반성이 "NO"를 내도 더 이상 강제로 인간 에스컬레이션시키지
    않는다 — 이전엔 이미 auto로 승급된 카테고리까지 매번 우회시켜 "승급은 사람만
    결정한다"는 Progressive Autonomy 원칙과 충돌했음(자가 반성 자체도 노이즈가 커서
    같은 명령어에 온도=0으로도 판정이 뒤바뀜을 확인함, 프롬프트 튜닝으로 해결 불가).
    대신 정상적으로 executor.py의 autonomy 게이트(auto/approve_then_execute)를
    타게 하고, 거부 사유는 reasoning에 남겨 승인 화면에서 사람이 참고하게 한다.
    """
    safe, rationale = _reflect_on_command(command, error_log, system_context)
    if safe:
        # 2026-09-12 Explainability 확장: 예전엔 "{backend} 추론 성공"이라는 내용
        # 없는 문구뿐이었음 — 이제 자가 반성이 실제로 판단한 근거를 담는다.
        reasoning = f"{backend} 추론 성공 — {rationale}" if rationale else f"{backend} 추론 성공"
    else:
        logging.warning(
            f"[자가 반성] {backend} 명령어에 우려 표명(강제 에스컬레이션 아님, "
            f"정상 게이트로 진행): {command}"
        )
        reasoning = f"⚠️ 자가 반성이 위험 판정({backend} 제안) — {rationale} — 승인 시 주의: {command}"
    return AgentResponse(
        error_category="LLM_Inferred", severity="CRITICAL",
        action_type=ActionType.EXECUTE_LLM_COMMAND,
        reasoning=reasoning,
        resolution_source="L2_LLM",
        command=command,
        l1_nearest_category=nearest_category,
        l1_nearest_distance=nearest_distance,
        l2_diagnosis=diagnosis,
        self_reflection_safe=safe,
    )


_EVIDENCE_SNIPPET_LEN = 100

# ChromaDB 문서의 "source" 메타데이터 → 사람이 읽을 수 있는 출처 라벨.
# 신뢰도 판단에 실제로 영향을 준다 — 사람이 직접 큐레이션/검증한 문서(카오스
# 인젝터 실측 문구 등)와 크롤링/증강으로 자동 생성된 문서, 그리고 온라인학습으로
# 자동 축적돼 트랙 레코드가 있는 문서는 신뢰해야 하는 정도가 다르다.
_SOURCE_LABELS: dict[str, str] = {
    "chaos_injector_signature":    "카오스 인젝터 실측 문구(사람이 직접 큐레이션)",
    "proactive_monitor_signature": "ProactiveMonitor 실측 문구(사람이 직접 큐레이션)",
    "github_v2":                   "GitHub 이슈 크롤링",
    "syslog_augment_v1":           "syslog 증강 데이터",
    "syslog_augment_v2":           "syslog 증강 데이터",
    "loghub_v1":                   "LogHub 공개 데이터셋",
}


def _format_track_record(meta: dict) -> str:
    """
    온라인학습(learn_from_feedback)으로 생성된 문서의 실행 트랙 레코드를
    사람이 읽을 수 있게 만든다(Explainability, 2026-09-11 추가) — 큐레이션
    데이터와 달리 이 문서는 실제 운영에서 반복 실행된 결과(success_count/
    failure_count, record_learned_outcome이 갱신)가 있어, "이 조치가 과거에
    실제로 몇 번 통했는가"를 신뢰도 판단에 직접 쓸 수 있다.
    """
    if meta.get("source") != "online_learning":
        return ""
    success = int(meta.get("success_count") or 0)
    failure = int(meta.get("failure_count") or 0)
    total   = success + failure
    if total == 0:
        return " [온라인학습, 실행 이력 없음]"
    return f" [온라인학습, 과거 {total}회 실행 중 {success}회 성공]"


def _confidence_label(best_dist: float) -> str:
    """
    최근접 거리와 L1 임계값(_RAG_THRESHOLD)의 비율로 신뢰도를 분류한다(Explainability,
    2026-09-12 추가). 지금까지는 "히트/미스" 이진 판정만 있어서, 임계값을 살짝
    넘겨서 겨우 통과한 애매한 매칭과 사실상 동일한 과거 사건을 구분하지 못했다 —
    사람이 승인 화면에서 신중히 볼지 말지 참고할 정보가 없었다.
    """
    ratio = best_dist / _RAG_THRESHOLD if _RAG_THRESHOLD else 0.0
    if ratio <= 0.2:
        return "매우 높음(거의 동일한 과거 사건)"
    if ratio <= 0.5:
        return "높음"
    if ratio <= 0.85:
        return "보통"
    return "낮음 — 임계값에 근접한 애매한 매칭, 신중히 검토할 것"


def _format_evidence(candidates: list[tuple[dict, float, str, str]],
                      top_action: str, best_id: str) -> str:
    """
    앙상블 투표에 실제로 참여한 후보들을 사람이 읽을 수 있게 정리한다(Explainability,
    2026-09-11 추가). "왜 이 조치를 골랐는가"에 벡터 검색이 실제로 근거 삼은 과거
    사건들을 그대로 보여줘, 승인 화면/대시보드에서 검증 가능하게 한다.

    후보는 거리순으로 정렬해 표시 — 투표 자체는 다수결이지만, 사람이 볼 땐 "가장
    가까운 것부터"가 직관적이다. 승리한 문서(best_id)에는 ✓ 표시. 출처(사람이
    큐레이션했는지, 크롤링/증강인지, 온라인학습으로 자동 축적됐는지)와 온라인학습
    문서의 실행 트랙 레코드도 같이 보여줘 신뢰도 판단에 쓸 수 있게 한다. 헤더에는
    최근접 거리 기반 신뢰도 라벨(_confidence_label, 2026-09-12 추가)도 붙인다.
    """
    vote_counts = Counter(m.get("action_type", "escalate_to_human") for m, _, _, _ in candidates)
    n_winning   = vote_counts[top_action]
    best_dist   = next(dist for _, dist, doc_id, _ in candidates if doc_id == best_id)
    lines = [
        f"L1 앙상블: 후보 {len(candidates)}개 중 {n_winning}개가 '{top_action}' 선택(다수결) "
        f"| 신뢰도: {_confidence_label(best_dist)} (최근접 거리 {best_dist:.4f} / 임계값 {_RAG_THRESHOLD:.2f})"
    ]
    for meta, dist, doc_id, doc_text in sorted(candidates, key=lambda c: c[1]):
        mark   = "✓" if doc_id == best_id else " "
        snippet = doc_text.strip().replace("\n", " ")[:_EVIDENCE_SNIPPET_LEN]
        source_label = _SOURCE_LABELS.get(meta.get("source"), "")
        track_record = _format_track_record(meta)
        suffix = f" [{source_label}]" if source_label else ""
        lines.append(
            f"  {mark} [거리 {dist:.4f}] {meta.get('action_type', '?')} ← {snippet}{suffix}{track_record}"
        )
    return "\n".join(lines)


def _ensemble_vote(
    candidates: list[tuple[dict, float, str, str]]
) -> tuple[dict, str, str]:
    """
    후보 목록에서 action_type 다수결로 최적 메타데이터를 선택한다.

    같은 action_type 후보 중 거리(distance)가 가장 작은 것을 반환한다.
    Counter는 모듈 레벨에서 import돼 매 호출마다 재임포트되지 않는다.

    후보는 (메타데이터, 거리, ChromaDB 문서 ID, 문서 원문) 튜플이며, 다수결로 뽑힌
    단일 문서의 ID와 사람이 읽을 수 있는 투표 근거 설명(evidence)도 같이 반환한다.
    ID는 실행 결과를 그 문서 하나에 정확히 되먹이는 데 쓰이고(record_learned_outcome
    참고), evidence는 Explainability용(_format_evidence 참고).
    """
    action_votes = Counter(m.get("action_type", "escalate_to_human") for m, _, _, _ in candidates)
    top_action   = action_votes.most_common(1)[0][0]

    best_meta, best_dist, best_id = None, float("inf"), None
    for meta, dist, doc_id, _ in candidates:
        if meta.get("action_type") == top_action and dist < best_dist:
            best_meta, best_dist, best_id = meta, dist, doc_id

    if best_meta is None:
        best_meta, _, best_id, _ = candidates[0]

    evidence = _format_evidence(candidates, top_action, best_id)
    return best_meta, best_id, evidence


# ── RAGEngine ─────────────────────────────────────────────────────────────────
class RAGEngine:
    """
    에러 로그를 분석해 적절한 복구 액션을 반환하는 핵심 추론 엔진.

    ChromaDB 싱글톤(_get_chroma_client)을 사용하므로, 복수의 RAGEngine 인스턴스를
    생성해도 DB 연결이 중복되지 않는다.
    """

    def __init__(self):
        logging.info("[RAGEngine] Vector DB 연결 초기화 중...")
        client = _get_chroma_client()
        try:
            self.collection = client.get_collection(name="error_playbook_vectors")
            logging.info(
                f"[RAGEngine] 연결 완료. "
                f"(현재 보유한 에러 지식: {self.collection.count()}개)"
            )
        except Exception as e:
            logging.warning(f"[RAGEngine] 콜렉션 없음, 새로 생성합니다: {e}")
            self.collection = client.get_or_create_collection(
                name="error_playbook_vectors"
            )
            logging.info("[RAGEngine] 빈 콜렉션 생성 완료. 추가 학습이 필요합니다.")
        _ollama_warmup()

    def _query_l1(self, log_texts: list[str]) -> dict:
        """ChromaDB 벡터 검색. 빈 컬렉션 TypeError 방어 포함."""
        try:
            return self.collection.query(query_texts=log_texts, n_results=5)
        except TypeError:
            # ChromaDB 0.5.x 버그: 빈 컬렉션 쿼리 시 TypeError 발생.
            n = len(log_texts)
            return {"documents": [[]] * n, "metadatas": [[]] * n, "distances": [[]] * n, "ids": [[]] * n}

    def _l2_slow_track(self, error_log: str, best_meta: dict, best_distance: float) -> AgentResponse:
        """L1 미스 시 Groq → Ollama → ipex_llm → Rule-based → Escalation 5단계 폴백 체인.

        best_meta/best_distance는 임계값(RAG_THRESHOLD)을 못 넘어 액션 채택은 포기한
        가장 가까운 L1 후보다 — 액션 결정에는 안 쓰지만, 그 카테고리 추측을
        AgentResponse.l1_nearest_category로 실어 보내 FP/FN 분석이 L2/RULE 경로도
        참고할 수 있게 한다(2026-09-15, run_fp_fn_analysis.py의 "L2 경로는 recall
        계산 자체가 불가능" 공백을 메우기 위함 — schemas.py의 필드 설명 참고).

        Groq 경로는 2026-09-15부터 진단→제안→검토 3단계 멀티에이전트 구조다:
        1단계 진단(_diagnose_error)이 원인/권장 조치 유형/대상을 먼저 구조화해
        뽑고, 2단계 제안(_run_groq)이 그 진단으로 보강된 컨텍스트로 실제 명령을
        생성하며, 3단계 검토(_make_llm_response 내부의 _reflect_on_command,
        기존 self-reflection 그대로)가 최종 안전성을 판정한다. Ollama/ipex_llm
        폴백은 실사용 빈도가 낮아 기존 단일 프롬프트 방식 그대로 유지한다.

        **3단계 검토의 독립성**: 2단계는 진단으로 보강된 enriched_context를 쓰지만,
        3단계 검토(self-reflection)는 일부러 원본 system_context만 받는다(코드
        review로 발견·수정, 2026-09-15) — 진단이 틀렸을 때 검토가 그 틀린 결론을
        그대로 다시 읽고 뭉개지 않도록, 제안과 검토가 서로 다른 정보만 공유하게
        분리했다.

        **진단 주도 라우팅(2026-09-17)**: 1단계 진단이 restart_service/kill_process
        (PID 아닌 대상만)/clear_memory를 확신 있게 뽑으면, 2·3단계(제안+검토)를
        건너뛰고 L1과 동일한 구조화 ActionType 경로로 바로 실행한다
        (`_route_from_diagnosis` 참고) — 지금까지는 진단 결과가 프롬프트 힌트로만
        쓰이고 실제 액션 결정엔 전혀 반영되지 않았다. 라우팅 불가 조건(대상이
        PID, action_type이 "other", 진단 자체 실패 등)에서는 기존 2·3단계 경로로
        그대로 폴백한다.
        """
        nearest_category = best_meta.get("error_category")
        logging.warning(
            f"[RAGEngine] 유사도 낮음 (거리: {best_distance:.4f}, 최근접 카테고리 추정: "
            f"{nearest_category}). Fallback 체인 시작..."
        )
        logging.info("🔍 [Observation] 시스템 상태 사전 진단을 시작합니다...")
        system_context = gather_system_context(error_log)
        logging.info(f"📊 [진단 완료] 수집된 컨텍스트 길이: {len(system_context)}자")

        # Step 1: Groq (1순위 — GROQ_API_KEY 설정 시)
        # 멀티에이전트 3단계(진단→제안→검토, 2026-09-15) — 명령 생성 전에 별도
        # LLM 호출로 원인/권장 조치/대상을 먼저 뽑아 시스템 컨텍스트에 보강한다.
        # 진단이 실패하거나 형식이 안 맞아도 diagnosis는 None이 되어 기존 방식
        # (진단 없이 바로 생성)으로 안전하게 폴백한다 — _diagnose_error 참고.
        groq_result = None
        if _is_groq_available():
            diagnosis = _diagnose_error(error_log, system_context)
            enriched_context = system_context
            diagnosis_summary = None
            if diagnosis:
                diagnosis_summary = (
                    f"추정 원인: {diagnosis['root_cause']} | 권장 조치 유형: "
                    f"{diagnosis['action_type']} | 대상: {diagnosis['target']}"
                )
                enriched_context = f"{system_context}\n[진단 에이전트 소견] {diagnosis_summary}"
                logging.info(f"[진단 에이전트] {diagnosis_summary}")

                structured = _route_from_diagnosis(
                    diagnosis, diagnosis_summary, nearest_category, best_distance,
                )
                if structured is not None:
                    return structured

            logging.info(f"[RAGEngine] Groq({GROQ_MODEL}) 추론 시작...")
            groq_result = _run_groq(error_log, enriched_context)
            if not groq_result.startswith("ERROR:"):
                logging.info(f"  👉 [Groq] 명령어: {groq_result}")
                # 2026-09-15 code-review 발견: 3단계 검토(self-reflection)에도
                # enriched_context를 넘기면 진단 에이전트 자신의 결론(잘못됐을 수도
                # 있는 원인/대상 추정)을 검토 단계가 그대로 다시 읽게 되어, "독립적
                # 재검증"이라는 설계 의도가 깨진다(진단이 틀렸으면 검토도 같이
                # 틀릴 위험 — faithfulness 실험이 잡으려던 것과 같은 부류의 문제).
                # 검토는 반드시 원본 system_context만 보게 한다.
                return _make_llm_response(
                    groq_result, error_log, system_context, "Groq",
                    nearest_category=nearest_category, nearest_distance=best_distance,
                    diagnosis=diagnosis_summary,
                )
            logging.warning(f"[RAGEngine] Groq 실패: {groq_result}")
        else:
            logging.info("[RAGEngine] GROQ_API_KEY 미설정. Ollama로 폴백...")

        # Step 2: Ollama
        llm_result = None
        if _is_ollama_available():
            logging.info(f"[RAGEngine] Ollama({OLLAMA_MODEL}) 추론 시작...")
            llm_result = _run_ollama(error_log, system_context)
            if not llm_result.startswith("ERROR:"):
                logging.info(f"  👉 [Ollama] 명령어: {llm_result}")
                return _make_llm_response(
                    llm_result, error_log, system_context, "Ollama",
                    nearest_category=nearest_category, nearest_distance=best_distance,
                )
            logging.warning(f"[RAGEngine] Ollama 실패: {llm_result}")
        else:
            logging.warning("[RAGEngine] Ollama 미실행. ipex_llm으로 시도...")

        # Step 3: ipex_llm
        ipex_result = run_ipex_engine(error_log, system_context)
        if ipex_result not in ("TIMEOUT", "ERROR") and not ipex_result.startswith("ERROR:"):
            logging.info(f"  👉 [ipex_llm] 명령어: {ipex_result}")
            return _make_llm_response(
                ipex_result, error_log, system_context, "ipex_llm",
                nearest_category=nearest_category, nearest_distance=best_distance,
            )
        logging.warning(f"[RAGEngine] ipex_llm 실패: {ipex_result}")

        # Step 4: Rule-based heuristic
        rule_match = _rule_based_fallback(error_log)
        if rule_match:
            rule_cmd, matched_desc = rule_match
            logging.info(f"  👉 [Rule Match] 명령어: {rule_cmd} (매칭: {matched_desc})")
            return AgentResponse(
                error_category="Rule_Inferred", severity="HIGH",
                action_type=ActionType.EXECUTE_RULE_COMMAND,
                reasoning=f"규칙 기반 매칭 — 키워드 감지: {matched_desc}",
                resolution_source="RULE",
                command=rule_cmd,
                l1_nearest_category=nearest_category,
                l1_nearest_distance=best_distance,
            )

        # Step 5: 완전 실패 → 인간 에스컬레이션
        return AgentResponse(
            error_category="Unknown", severity="HIGH",
            action_type=ActionType.ESCALATE_TO_HUMAN,
            reasoning=(
                f"모든 Fallback 실패 "
                f"(Groq: {groq_result}, Ollama: {llm_result}, ipex: {ipex_result})"
            ),
            resolution_source="L2_LLM",
            l1_nearest_category=nearest_category,
            l1_nearest_distance=best_distance,
        )

    def analyze_error(self, log_text: str) -> AgentResponse:
        """단일 에러 로그를 분석해 AgentResponse를 반환한다."""
        logging.info("[RAGEngine] 에러 로그 벡터 유사도 검색 시작...")
        start   = time.perf_counter()
        results = self._query_l1([log_text])
        logging.info(
            f"[RAGEngine] Vector DB 검색 완료 (소요시간: {time.perf_counter() - start:.4f}초)"
        )

        docs  = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]
        ids   = results["ids"][0]

        if not docs:
            return AgentResponse(
                error_category="Unknown", severity="MEDIUM",
                action_type=ActionType.ESCALATE_TO_HUMAN,
                reasoning="Vector DB가 비어있거나 검색에 실패했습니다.",
                resolution_source="L1_CACHE",
            )

        candidates = [
            (metas[i], dists[i], ids[i], docs[i])
            for i in range(len(dists)) if dists[i] <= _RAG_THRESHOLD
        ]
        logging.info(f"  [매칭된 과거 에러] {docs[0][:60]}... (거리: {dists[0]:.4f})")

        if candidates:
            best_meta, best_id, evidence = _ensemble_vote(candidates)
            logging.info(
                f"  [앙상블] {len(candidates)}/{len(dists)}개 후보 "
                f"→ 다수결 action: {best_meta.get('action_type')}"
            )
            return _build_response_from_meta(best_meta, "L1_CACHE", doc_id=best_id, evidence=evidence)

        return self._l2_slow_track(log_text, metas[0], dists[0])

    def analyze_errors_batch(self, log_texts: list[str]) -> list[AgentResponse]:
        """
        N개의 에러 로그를 ChromaDB 단일 쿼리로 처리한다.

        L1 히트: 배치 내 앙상블 응답 즉시 생성.
        L1 미스: _l2_slow_track()으로 직접 전달 (중복 DB 쿼리 없음).
        """
        if not log_texts:
            return []

        logging.info(f"[RAGEngine] 배치 쿼리 시작: {len(log_texts)}건")
        start   = time.perf_counter()
        results = self._query_l1(log_texts)
        logging.info(
            f"[RAGEngine] 배치 Vector DB 검색 완료 "
            f"({len(log_texts)}건 / {time.perf_counter() - start:.4f}초)"
        )

        responses: list[AgentResponse] = []
        for i, log_text in enumerate(log_texts):
            docs  = results["documents"][i]
            metas = results["metadatas"][i]
            dists = results["distances"][i]
            ids   = results["ids"][i]

            if not docs:
                responses.append(AgentResponse(
                    error_category="Unknown", severity="MEDIUM",
                    action_type=ActionType.ESCALATE_TO_HUMAN,
                    reasoning="Vector DB가 비어있거나 검색에 실패했습니다.",
                    resolution_source="L1_CACHE",
                ))
                continue

            candidates = [
                (metas[j], dists[j], ids[j], docs[j])
                for j in range(len(dists)) if dists[j] <= _RAG_THRESHOLD
            ]

            if not candidates:
                logging.info(f"  [배치 {i + 1}/{len(log_texts)}] L1 미스 → slow track")
                responses.append(self._l2_slow_track(log_text, metas[0], dists[0]))
                continue

            best_meta, best_id, evidence = _ensemble_vote(candidates)
            logging.info(
                f"  [배치 {i + 1}/{len(log_texts)}] 앙상블 {len(candidates)}개 후보 "
                f"→ {best_meta.get('action_type')}"
            )
            responses.append(
                _build_response_from_meta(best_meta, "L1_CACHE", doc_id=best_id, evidence=evidence)
            )

        return responses

    def learn_from_feedback(self, error_log: str, successful_command: str) -> None:
        """
        L2/Rule 성공 명령어를 L1 Cache(ChromaDB)에 upsert해 지속 학습한다.

        source="online_learning" 태깅으로 다른 데이터 유입 경로(GitHub 크롤링,
        카오스 시그니처 등)와 출처를 구분한다(add_chaos_injector_signatures.py 등과 동일 컨벤션).
        같은 에러 텍스트에 같은 커맨드가 다시 학습되면(반복 성공) success_count를
        누적하고, 다른 커맨드로 대체되면(같은 에러에 새 해결책) 카운터를 리셋한다 —
        record_learned_outcome()이 이 카운터를 보고 반복 실패 엔트리를 제거한다.
        """
        doc_id = f"learned_{hashlib.md5(error_log.encode('utf-8')).hexdigest()}"

        prev_meta = None
        try:
            existing = self.collection.get(ids=[doc_id])
            if existing.get("ids"):
                prev_meta = existing["metadatas"][0]
        except Exception:
            logging.error(f"[온라인학습] 기존 엔트리 조회 실패:\n{traceback.format_exc()}")

        if prev_meta and prev_meta.get("command") == successful_command:
            success_count = int(prev_meta.get("success_count", 0)) + 1
            failure_count = int(prev_meta.get("failure_count", 0))
        else:
            success_count = 1
            failure_count = 0

        try:
            self.collection.upsert(
                ids=[doc_id],
                documents=[error_log],
                metadatas=[{
                    "error_category": "Learned_from_LLM",
                    "action_type":    ActionType.EXECUTE_LLM_COMMAND.value,
                    "command":        successful_command,
                    "reasoning":      f"L2 학습 성공 명령어: {successful_command}",
                    "target_process": "unknown",
                    "learned_at":     int(time.time()),
                    "source":         "online_learning",
                    "success_count":  success_count,
                    "failure_count":  failure_count,
                }],
            )
            logging.info(f"[온라인학습] 지식 학습 완료 (ID={doc_id[:16]}, 성공 {success_count}회)")
            logging.info(f"  에러: {error_log[:50]}...")
            logging.info(f"  해결: {successful_command}")
        except Exception:
            logging.error(f"[온라인학습] Vector DB 학습 실패:\n{traceback.format_exc()}")

    def record_learned_outcome(self, doc_id: str, success: bool) -> None:
        """
        온라인학습(learn_from_feedback)으로 생성된 L1 엔트리가 L1_CACHE 히트로
        실제 실행된 결과를 되먹여 success_count/failure_count를 갱신한다.

        source가 "online_learning"이 아닌 엔트리(GitHub 크롤링·카오스 시그니처 등
        사람이 직접 큐레이션한 데이터)는 런타임 실행 결과로 건드리지 않는다 —
        이 엔트리들의 신뢰도는 별도 검증 절차(FP/FN 분석 등)로 관리한다.

        최소 실패 건수(_LEARNED_ENTRY_MAX_FAILURES)와 실패율
        (_LEARNED_ENTRY_FAILURE_RATE_THRESHOLD)을 모두 넘으면 엔트리를 삭제해
        다음 히트부터 L2가 새 해결책을 다시 시도하게 한다.
        """
        try:
            existing = self.collection.get(ids=[doc_id])
        except Exception:
            logging.error(f"[온라인학습] 결과 되먹임 중 조회 실패:\n{traceback.format_exc()}")
            return

        if not existing.get("ids"):
            logging.warning(f"[온라인학습] 되먹임 대상 엔트리 없음(이미 삭제됐을 수 있음): {doc_id}")
            return

        meta = existing["metadatas"][0]
        if meta.get("source") != "online_learning":
            return

        success_count = int(meta.get("success_count", 0))
        failure_count = int(meta.get("failure_count", 0))
        if success:
            success_count += 1
        else:
            failure_count += 1

        total        = success_count + failure_count
        failure_rate = (failure_count / total) if total else 0.0

        if (failure_count >= _LEARNED_ENTRY_MAX_FAILURES
                and failure_rate > _LEARNED_ENTRY_FAILURE_RATE_THRESHOLD):
            try:
                self.collection.delete(ids=[doc_id])
                logging.warning(
                    f"[온라인학습] 반복 실패로 엔트리 제거: {doc_id[:24]} "
                    f"(성공 {success_count} / 실패 {failure_count})"
                )
            except Exception:
                logging.error(f"[온라인학습] 엔트리 삭제 실패:\n{traceback.format_exc()}")
            return

        meta["success_count"] = success_count
        meta["failure_count"] = failure_count
        try:
            self.collection.update(ids=[doc_id], metadatas=[meta])
            logging.info(
                f"[온라인학습] 결과 되먹임: {doc_id[:24]} "
                f"(성공 {success_count} / 실패 {failure_count})"
            )
        except Exception:
            logging.error(f"[온라인학습] 카운터 갱신 실패:\n{traceback.format_exc()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine     = RAGEngine()
    test_error = "OOM killer invoked for nginx"
    print("\n--- [사전 진단 연동 테스트] ---")
    response = engine.analyze_error(test_error)
    print(f"\n최종 결과: {response.reasoning}")
