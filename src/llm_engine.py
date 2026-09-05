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
# qwen/qwen3.6-27b + reasoning_effort=none 조합이 사고형(thinking) 오버헤드 없이
# 안정적으로 원샷 명령어를 반환하는 것으로 검증됨 — gpt-oss 계열은 harmony 포맷상
# reasoning 채널을 강제로 소비해 max_tokens 내에서 content가 비는 문제가 있었음.
GROQ_MODEL          = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
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


def _rule_based_fallback(error_log: str) -> str | None:
    """에러 로그 키워드로 규칙 기반 명령어를 반환한다. 미매칭 시 None."""
    lower = error_log.lower()
    for keywords, command in _ERROR_RULES:
        if any(kw in lower for kw in keywords):
            return command
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


def _reflect_on_command(command: str, error_log: str, system_ctx: str) -> bool:
    """
    자가 반성 루프 — LLM이 생성한 명령어의 안전성을 재검증한다.

    조회성 명령어(_is_read_only_command)는 LLM 호출 없이 항상 통과시킨다.
    나머지는 Groq(GROQ_API_KEY 설정 시) 우선 사용, 실패/미설정 시 Ollama로 폴백한다.
    YES → 실행 허용 / NO 또는 오류 → 에스컬레이션으로 전환.
    검증 실패(네트워크 오류 등) 시 보수적으로 True 반환한다.
    최종 방어선은 executor.py의 화이트리스트 검증이므로 이중 안전망이 유지된다.
    """
    if _is_read_only_command(command):
        logging.info(f"[자가 반성] '{command}' — 조회성 명령어, LLM 판정 없이 통과")
        return True

    prompt = (
        "You are a safety reviewer for a Self-Healing MLOps Agent.\n"
        "The proposed command has ALREADY passed a strict security whitelist — only a small\n"
        "fixed set of safe commands is even possible here (systemctl restart/stop/start/status,\n"
        "pkill/kill with soft signals only, memory/disk/process read-only queries). Shell\n"
        "chaining, arbitrary paths, and destructive tools are already blocked before this point.\n"
        "Your job is NOT to re-judge generic shell danger. Judge only whether this specific\n"
        "action is a reasonable, proportionate response to the described error.\n"
        "Reply with only YES or NO.\n\n"
        f"Error: {error_log[:200]}\n"
        f"Proposed command: {command}\n"
        f"System: {system_ctx}\n\n"
        "Is this command a reasonable response to the error? (YES/NO):"
    )

    if _is_groq_available():
        raw = _call_groq_chat(prompt, max_tokens=4, timeout=15)
        if not raw.startswith("ERROR:"):
            answer = raw.strip().upper()
            safe   = answer.startswith("YES")
            logging.info(
                f"[자가 반성/Groq] 명령어='{command}' | 판정='{answer}' "
                f"→ {'통과' if safe else '거부'}"
            )
            return safe
        logging.warning(f"[자가 반성] Groq 검증 실패, Ollama로 폴백: {raw}")

    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0, "num_predict": 4},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            answer = json.loads(resp.read()).get("response", "").strip().upper()
            safe   = answer.startswith("YES")
            logging.info(
                f"[자가 반성/Ollama] 명령어='{command}' | 판정='{answer}' "
                f"→ {'통과' if safe else '거부'}"
            )
            return safe
    except Exception as e:
        logging.warning(f"[자가 반성] 검증 요청 실패 — 보수적 통과 처리: {e}")
        return True


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
def _build_response_from_meta(meta: dict, source: str) -> AgentResponse:
    """ChromaDB 메타데이터 dict → AgentResponse. L1 fast track과 배치 쿼리가 공유."""
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
        reasoning=meta.get("reasoning", "No reasoning found in DB"),
        resolution_source=source,
    )


def _make_llm_response(command: str, error_log: str, system_context: str, backend: str) -> AgentResponse:
    """
    자가 반성 결과를 반영해 EXECUTE_LLM_COMMAND AgentResponse를 만든다.

    2026-09-05 결정: 자가 반성이 "NO"를 내도 더 이상 강제로 인간 에스컬레이션시키지
    않는다 — 이전엔 이미 auto로 승급된 카테고리까지 매번 우회시켜 "승급은 사람만
    결정한다"는 Progressive Autonomy 원칙과 충돌했음(자가 반성 자체도 노이즈가 커서
    같은 명령어에 온도=0으로도 판정이 뒤바뀜을 확인함, 프롬프트 튜닝으로 해결 불가).
    대신 정상적으로 executor.py의 autonomy 게이트(auto/approve_then_execute)를
    타게 하고, 거부 사유는 reasoning에 남겨 승인 화면에서 사람이 참고하게 한다.
    """
    if _reflect_on_command(command, error_log, system_context):
        reasoning = f"{backend} 추론 성공"
    else:
        logging.warning(
            f"[자가 반성] {backend} 명령어에 우려 표명(강제 에스컬레이션 아님, "
            f"정상 게이트로 진행): {command}"
        )
        reasoning = f"⚠️ 자가 반성이 위험 판정({backend} 제안) — 승인 시 주의: {command}"
    return AgentResponse(
        error_category="LLM_Inferred", severity="CRITICAL",
        action_type=ActionType.EXECUTE_LLM_COMMAND,
        reasoning=reasoning,
        resolution_source="L2_LLM",
        command=command,
    )


def _ensemble_vote(candidates: list[tuple[dict, float]]) -> dict:
    """
    후보 목록에서 action_type 다수결로 최적 메타데이터를 선택한다.

    같은 action_type 후보 중 거리(distance)가 가장 작은 것을 반환한다.
    Counter는 모듈 레벨에서 import돼 매 호출마다 재임포트되지 않는다.
    """
    action_votes = Counter(m.get("action_type", "escalate_to_human") for m, _ in candidates)
    top_action   = action_votes.most_common(1)[0][0]

    best_meta, best_dist = None, float("inf")
    for meta, dist in candidates:
        if meta.get("action_type") == top_action and dist < best_dist:
            best_meta, best_dist = meta, dist

    return best_meta if best_meta is not None else candidates[0][0]


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
            return {"documents": [[]] * n, "metadatas": [[]] * n, "distances": [[]] * n}

    def _l2_slow_track(self, error_log: str, best_distance: float) -> AgentResponse:
        """L1 미스 시 Groq → Ollama → ipex_llm → Rule-based → Escalation 5단계 폴백 체인."""
        logging.warning(
            f"[RAGEngine] 유사도 낮음 (거리: {best_distance:.4f}). Fallback 체인 시작..."
        )
        logging.info("🔍 [Observation] 시스템 상태 사전 진단을 시작합니다...")
        system_context = gather_system_context(error_log)
        logging.info(f"📊 [진단 완료] 수집된 컨텍스트 길이: {len(system_context)}자")

        # Step 1: Groq (1순위 — GROQ_API_KEY 설정 시)
        groq_result = None
        if _is_groq_available():
            logging.info(f"[RAGEngine] Groq({GROQ_MODEL}) 추론 시작...")
            groq_result = _run_groq(error_log, system_context)
            if not groq_result.startswith("ERROR:"):
                logging.info(f"  👉 [Groq] 명령어: {groq_result}")
                return _make_llm_response(groq_result, error_log, system_context, "Groq")
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
                return _make_llm_response(llm_result, error_log, system_context, "Ollama")
            logging.warning(f"[RAGEngine] Ollama 실패: {llm_result}")
        else:
            logging.warning("[RAGEngine] Ollama 미실행. ipex_llm으로 시도...")

        # Step 3: ipex_llm
        ipex_result = run_ipex_engine(error_log, system_context)
        if ipex_result not in ("TIMEOUT", "ERROR") and not ipex_result.startswith("ERROR:"):
            logging.info(f"  👉 [ipex_llm] 명령어: {ipex_result}")
            return _make_llm_response(ipex_result, error_log, system_context, "ipex_llm")
        logging.warning(f"[RAGEngine] ipex_llm 실패: {ipex_result}")

        # Step 4: Rule-based heuristic
        rule_cmd = _rule_based_fallback(error_log)
        if rule_cmd:
            logging.info(f"  👉 [Rule Match] 명령어: {rule_cmd}")
            return AgentResponse(
                error_category="Rule_Inferred", severity="HIGH",
                action_type=ActionType.EXECUTE_RULE_COMMAND,
                reasoning="규칙 기반 키워드 매칭",
                resolution_source="RULE",
                command=rule_cmd,
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

        if not docs:
            return AgentResponse(
                error_category="Unknown", severity="MEDIUM",
                action_type=ActionType.ESCALATE_TO_HUMAN,
                reasoning="Vector DB가 비어있거나 검색에 실패했습니다.",
                resolution_source="L1_CACHE",
            )

        candidates = [
            (metas[i], dists[i]) for i in range(len(dists)) if dists[i] <= _RAG_THRESHOLD
        ]
        logging.info(f"  [매칭된 과거 에러] {docs[0][:60]}... (거리: {dists[0]:.4f})")

        if candidates:
            best_meta = _ensemble_vote(candidates)
            logging.info(
                f"  [앙상블] {len(candidates)}/{len(dists)}개 후보 "
                f"→ 다수결 action: {best_meta.get('action_type')}"
            )
            return _build_response_from_meta(best_meta, "L1_CACHE")

        return self._l2_slow_track(log_text, dists[0])

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

            if not docs:
                responses.append(AgentResponse(
                    error_category="Unknown", severity="MEDIUM",
                    action_type=ActionType.ESCALATE_TO_HUMAN,
                    reasoning="Vector DB가 비어있거나 검색에 실패했습니다.",
                    resolution_source="L1_CACHE",
                ))
                continue

            candidates = [
                (metas[j], dists[j]) for j in range(len(dists)) if dists[j] <= _RAG_THRESHOLD
            ]

            if not candidates:
                logging.info(f"  [배치 {i + 1}/{len(log_texts)}] L1 미스 → slow track")
                responses.append(self._l2_slow_track(log_text, dists[0]))
                continue

            best_meta = _ensemble_vote(candidates)
            logging.info(
                f"  [배치 {i + 1}/{len(log_texts)}] 앙상블 {len(candidates)}개 후보 "
                f"→ {best_meta.get('action_type')}"
            )
            responses.append(_build_response_from_meta(best_meta, "L1_CACHE"))

        return responses

    def learn_from_feedback(self, error_log: str, successful_command: str) -> None:
        """L2 성공 명령어를 L1 Cache(ChromaDB)에 upsert해 지속 학습한다."""
        doc_id = f"learned_{hashlib.md5(error_log.encode('utf-8')).hexdigest()}"
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
                }],
            )
            logging.info(f"[Phase 4] 지식 학습 완료 (ID={doc_id[:16]})")
            logging.info(f"  에러: {error_log[:50]}...")
            logging.info(f"  해결: {successful_command}")
        except Exception:
            logging.error(f"[Phase 4] Vector DB 학습 실패:\n{traceback.format_exc()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine     = RAGEngine()
    test_error = "OOM killer invoked for nginx"
    print("\n--- [사전 진단 연동 테스트] ---")
    response = engine.analyze_error(test_error)
    print(f"\n최종 결과: {response.reasoning}")
