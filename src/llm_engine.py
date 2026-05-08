import hashlib
import json
import logging
import multiprocessing as mp
import os
import sys
import time
import traceback
import urllib.error
import urllib.request

import chromadb
from chromadb.config import Settings

from src.system_diagnostics import gather_system_context

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.schemas import AgentResponse, ActionType

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")

# =====================================================================
# [Rule-based Heuristic Fallback]
# ipex_llm 없이도 동작하는 키워드 기반 명령어 매핑.
# Slow Track에서 LLM 실패 시 마지막 수단으로 사용.
# =====================================================================
_ERROR_RULES: list[tuple[tuple[str, ...], str]] = [
    (("out of memory", "oom killer", "cannot allocate memory", "cuda out of memory", "vram"), "pkill -f python"),
    (("no space left on device", "disk full", "disk space"), "df -h"),
    (("address already in use", "bind() failed", "port 80", "port 443"), "systemctl restart nginx"),
    (("nginx",), "systemctl restart nginx"),
    (("postgresql", "postgres"), "systemctl restart postgresql"),
    (("too many open files",), "ulimit -n 65536"),
    (("connection refused", "connection timeout"), "ss -tuln"),
]

def _rule_based_fallback(error_log: str) -> str | None:
    lower = error_log.lower()
    for keywords, command in _ERROR_RULES:
        if any(kw in lower for kw in keywords):
            return command
    return None


# =====================================================================
# [공통 프롬프트 빌더]
# =====================================================================
def _build_prompt(error_log: str, system_context: str) -> str:
    return f"""You are a Self-Healing MLOps Agent. Reply with ONE raw Linux command only. No markdown, no backticks, no explanation, no sudo.

Error: nginx bind() to 0.0.0.0:80 failed
Command: systemctl restart nginx

Error: CUDA out of memory
Command: pkill -f python

Error: no space left on device
Command: df -h

Error: too many open files
Command: ulimit -n 65536

System: {system_context}
Error: {error_log}
Command:"""


_PROSE_STARTERS = {
    "to", "in", "please", "you", "first", "the", "this", "here", "note",
    "i", "we", "it", "if", "use", "run", "try", "make", "sure", "for",
}

def _clean_llm_output(raw: str) -> str:
    """마크다운, sudo, 영문 산문을 제거하고 첫 번째 유효 셸 명령어만 반환."""
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("```") or line.startswith("#"):
            continue
        line = line.removeprefix("bash").strip()
        if line.startswith("sudo "):
            line = line[5:].strip()
        # 번호 목록 (1. 2) 등), 마크다운 볼드(**), 산문 동사/관사 제거
        first_token = line.split()[0] if line.split() else ""
        if first_token.startswith("**") or first_token[:-1].isdigit():
            continue
        if first_token.lower().rstrip(".,:") in _PROSE_STARTERS:
            continue
        return line
    return ""


# =====================================================================
# [Step 1] Ollama (로컬 HTTP API — CPU/GPU 무관, 설치만 하면 동작)
# =====================================================================
def _run_ollama(error_log: str, system_context: str, timeout: int = 60) -> str:
    """Ollama API를 호출해 명령어를 추론한다. 실패 시 'ERROR:...' 반환."""
    prompt = _build_prompt(error_log, system_context)
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 24},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            command = _clean_llm_output(result.get("response", ""))
            if command:
                return command
            return "ERROR: Ollama returned empty response"
    except urllib.error.URLError as e:
        return f"ERROR: Ollama not reachable - {e}"
    except Exception as e:
        return f"ERROR: Ollama call failed - {e}"


def _reflect_on_command(command: str, error_log: str, system_ctx: str) -> bool:
    """
    자가 반성 루프 — LLM이 생성한 명령어의 안전성을 동일 LLM으로 재검증.
    YES → 실행 허용 / NO 또는 오류 → 에스컬레이션으로 전환.
    검증 실패(네트워크 오류 등) 시 보수적으로 True 반환 (executor 보안 필터가 최종 방어).
    """
    prompt = (
        "You are a safety reviewer for a Self-Healing MLOps Agent.\n"
        "Evaluate if the proposed command is safe and appropriate for fixing the error.\n"
        "Reply with only YES or NO.\n\n"
        f"Error: {error_log[:200]}\n"
        f"Proposed command: {command}\n"
        f"System: {system_ctx}\n\n"
        "Is this command safe and appropriate? (YES/NO):"
    )
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
            safe = answer.startswith("YES")
            logging.info(
                f"[자가 반성] 명령어='{command}' | 판정='{answer}' → {'통과' if safe else '거부'}"
            )
            return safe
    except Exception as e:
        logging.warning(f"[자가 반성] 검증 요청 실패 — 보수적 통과 처리: {e}")
        return True


def _is_ollama_available() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=2)
        return True
    except Exception:
        return False


# =====================================================================
# [Step 2] ipex_llm (Intel Arc GPU 환경 전용)
# =====================================================================
mp_ctx = mp.get_context("spawn")


def _ipex_inference_worker(conn, error_log, system_context):
    import sys
    import warnings
    import traceback

    warnings.filterwarnings("ignore")
    try:
        from ipex_llm.transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer

        hf_model_id = "Qwen/Qwen2.5-3B-Instruct"
        tokenizer = AutoTokenizer.from_pretrained(hf_model_id)
        model = AutoModelForCausalLM.from_pretrained(
            hf_model_id, load_in_4bit=True, optimize_model=True,
        ).to("cpu")

        prompt = _build_prompt(error_log, system_context)
        encoded = tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
        outputs = model.generate(
            input_ids=encoded["input_ids"],
            attention_mask=encoded["attention_mask"],
            max_new_tokens=24,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
        generated = outputs[0][encoded["input_ids"].size(1):]
        command = _clean_llm_output(tokenizer.decode(generated, skip_special_tokens=True))
        conn.send({"status": "success", "result": command})
    except Exception as e:
        conn.send({"status": "error", "reason": f"{e}\n{traceback.format_exc()}"})
    finally:
        conn.close()
        os._exit(0)   # spawn 프로세스 즉시 종료 → VRAM 즉시 반환 (sys.exit은 finalizer 거침)


def run_ipex_engine(error_log: str, system_context: str, timeout: int = 600) -> str:
    parent_conn, child_conn = mp_ctx.Pipe()
    p = mp_ctx.Process(target=_ipex_inference_worker, args=(child_conn, error_log, system_context))
    p.start()

    if parent_conn.poll(timeout):
        response = parent_conn.recv()
        p.join()
        return response["result"] if response["status"] == "success" else f"ERROR: {response['reason']}"
    else:
        logging.error("[ipex_llm] Timeout! 프로세스 강제 종료.")
        p.terminate()
        p.join()
        return "TIMEOUT"


# =====================================================================
# [메인 엔진 클래스]
# =====================================================================
class RAGEngine:
    def __init__(self):
        logging.info("[RAGEngine] Vector DB 연결 초기화 중...")
        persist_directory = os.path.join(os.getcwd(), "data", "chroma_db")
        self.chroma_client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False)
        )

        try:
            self.collection = self.chroma_client.get_collection(name="error_playbook_vectors")
            logging.info(f"[RAGEngine] 연결 완료. (현재 보유한 에러 지식: {self.collection.count()}개)")
        except Exception as e:
            logging.warning(f"[RAGEngine] 콜렉션 없음, 새로 생성합니다: {e}")
            self.collection = self.chroma_client.get_or_create_collection(name="error_playbook_vectors")
            logging.info("[RAGEngine] 빈 콜렉션 생성 완료. 추가 학습이 필요합니다.")

    def analyze_error(self, log_text: str) -> AgentResponse:
        logging.info("[RAGEngine] 에러 로그 벡터 유사도 검색 시작...")
        start_time = time.perf_counter()

        results = self.collection.query(query_texts=[log_text], n_results=1)

        latency = time.perf_counter() - start_time
        logging.info(f"[RAGEngine] Vector DB 검색 완료 (소요시간: {latency:.4f}초)")

        if not results["documents"][0]:
            return AgentResponse(
                error_category="Unknown",
                severity="MEDIUM",
                action_type=ActionType.ESCALATE_TO_HUMAN,
                reasoning="Vector DB가 비어있거나 검색에 실패했습니다.",
            )

        best_match_doc = results["documents"][0][0]
        best_match_meta = results["metadatas"][0][0]
        distance = results["distances"][0][0]

        logging.info(f"  👉 [매칭된 과거 에러] {best_match_doc[:60]}... (거리: {distance:.4f})")

        # =================================================================
        # 2. [Slow Track] 모르는 에러 처리 (Fallback LLM 개입 + 사전 진단)
        # =================================================================
        if distance > 0.8:
            logging.warning(f"[RAGEngine] 유사도 낮음 (거리: {distance:.4f}). Fallback 체인 시작...")

            logging.info("🔍 [Observation] 시스템 상태 사전 진단을 시작합니다...")
            current_system_state = gather_system_context(log_text)
            logging.info(f"📊 [진단 완료] 수집된 컨텍스트 길이: {len(current_system_state)}자")

            # --- Step 1: Ollama (CPU/GPU 무관, 설치만 하면 동작) ---
            llm_result = None
            if _is_ollama_available():
                logging.info(f"[RAGEngine] Ollama({OLLAMA_MODEL}) 추론 시작...")
                llm_result = _run_ollama(log_text, current_system_state)
                if not llm_result.startswith("ERROR:"):
                    logging.info(f"  👉 [Ollama] 명령어: {llm_result}")
                    if not _reflect_on_command(llm_result, log_text, current_system_state):
                        logging.warning(f"[자가 반성] Ollama 명령어 거부 → 인간 에스컬레이션: {llm_result}")
                        return AgentResponse(
                            error_category="Unknown",
                            severity="HIGH",
                            action_type=ActionType.ESCALATE_TO_HUMAN,
                            reasoning=f"[자가 반성 거부] Ollama 제안 명령어 위험 판정: {llm_result}",
                        )
                    return AgentResponse(
                        error_category="LLM_Inferred",
                        severity="CRITICAL",
                        action_type=ActionType.EXECUTE_LLM_COMMAND,
                        reasoning=f"[Ollama 추론 (L2)] {llm_result}",
                    )
                logging.warning(f"[RAGEngine] Ollama 실패: {llm_result}")
            else:
                logging.warning("[RAGEngine] Ollama 미실행. ipex_llm으로 시도...")

            # --- Step 2: ipex_llm (Intel Arc GPU 환경) ---
            ipex_result = run_ipex_engine(log_text, current_system_state)
            if ipex_result not in ["TIMEOUT", "ERROR"] and not ipex_result.startswith("ERROR:"):
                logging.info(f"  👉 [ipex_llm] 명령어: {ipex_result}")
                if not _reflect_on_command(ipex_result, log_text, current_system_state):
                    logging.warning(f"[자가 반성] ipex 명령어 거부 → 인간 에스컬레이션: {ipex_result}")
                    return AgentResponse(
                        error_category="Unknown",
                        severity="HIGH",
                        action_type=ActionType.ESCALATE_TO_HUMAN,
                        reasoning=f"[자가 반성 거부] ipex 제안 명령어 위험 판정: {ipex_result}",
                    )
                return AgentResponse(
                    error_category="LLM_Inferred",
                    severity="CRITICAL",
                    action_type=ActionType.EXECUTE_LLM_COMMAND,
                    reasoning=f"[LLM 추론 (L2)] {ipex_result}",
                )
            logging.warning(f"[RAGEngine] ipex_llm 실패: {ipex_result}")

            # --- Step 3: Rule-based heuristic (의존성 없음) ---
            rule_cmd = _rule_based_fallback(log_text)
            if rule_cmd:
                logging.info(f"  👉 [Rule Match] 명령어: {rule_cmd}")
                return AgentResponse(
                    error_category="Rule_Inferred",
                    severity="HIGH",
                    action_type=ActionType.EXECUTE_LLM_COMMAND,
                    reasoning=f"[규칙 기반 추론] {rule_cmd}",
                )

            # --- Step 4: 완전 실패 → 인간 에스컬레이션 ---
            return AgentResponse(
                error_category="Unknown",
                severity="HIGH",
                action_type=ActionType.ESCALATE_TO_HUMAN,
                reasoning=f"모든 Fallback 실패 (Ollama: {llm_result}, ipex: {ipex_result})",
            )

        # =================================================================
        # 3. [Fast Track] 아는 에러 처리 (Vector DB 기반 즉각 조치)
        # =================================================================
        action_str = best_match_meta.get("action_type", "escalate_to_human")
        try:
            action_enum = ActionType(action_str)
        except ValueError:
            action_enum = ActionType.ESCALATE_TO_HUMAN

        reasoning_text = best_match_meta.get("reasoning", "No reasoning found in DB")
        reasoning_with_l1_marker = f"[Vector DB 유사도 매칭 성공] {reasoning_text}"

        return AgentResponse(
            error_category=best_match_meta.get("error_category", "Unknown"),
            severity="HIGH",
            action_type=action_enum,
            reasoning=reasoning_with_l1_marker,
        )

    def learn_from_feedback(self, error_log: str, successful_command: str) -> None:
        # executor._save_to_l1_cache()와 동일한 MD5 기반 ID → upsert로 중복 방지
        doc_id = f"learned_{hashlib.md5(error_log.encode('utf-8')).hexdigest()}"

        try:
            self.collection.upsert(
                ids=[doc_id],
                documents=[error_log],
                metadatas=[{
                    "error_category": "Learned_from_LLM",
                    "action_type": ActionType.EXECUTE_LLM_COMMAND.value,
                    "reasoning": successful_command,
                    "target_process": "unknown",
                    "learned_at": int(time.time()),
                }],
            )
            logging.info(f"[Phase 4] 지식 학습 완료 (ID={doc_id[:16]})")
            logging.info(f"  에러: {error_log[:50]}...")
            logging.info(f"  해결: {successful_command}")
        except Exception:
            logging.error(f"[Phase 4] Vector DB 학습 실패:\n{traceback.format_exc()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine = RAGEngine()
    
    # 더미 에러로 테스트
    test_error = "OOM killer invoked for nginx"
    print("\n--- [사전 진단 연동 테스트] ---")
    response = engine.analyze_error(test_error)
    print(f"\n최종 결과: {response.reasoning}")