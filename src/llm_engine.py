import hashlib
import json
import logging
import multiprocessing as mp
import os
import threading
import time
import traceback
import urllib.error
import urllib.request

import chromadb
from chromadb.config import Settings

from src.system_diagnostics import gather_system_context
from src.schemas import AgentResponse, ActionType

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL        = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
_OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "3"))
_OLLAMA_RETRY_BASE  = float(os.getenv("OLLAMA_RETRY_BASE_SEC", "2.0"))
_OLLAMA_KEEP_ALIVE  = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
# L1 캐시 적중 판별 거리 임계치 — 낮을수록 엄격
_RAG_THRESHOLD      = float(os.getenv("RAG_THRESHOLD", "1.2"))

# ── ChromaDB Singleton ────────────────────────────────────────────────────────
# 프로세스 내 클라이언트를 하나만 유지해서 파일 락 경합을 방지한다.
# Double-Checked Locking: 1차 검사(락 없이)는 초기화 완료 후 빠른 경로.
_chroma_client = None
_chroma_lock   = threading.Lock()


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        with _chroma_lock:
            if _chroma_client is None:
                persist_dir = os.path.join(os.getcwd(), "data", "chroma_db")
                _chroma_client = chromadb.PersistentClient(
                    path=persist_dir,
                    settings=Settings(anonymized_telemetry=False),
                )
                logging.info("[ChromaDB] Singleton 클라이언트 초기화 완료.")
    return _chroma_client


# ── Rule-based Heuristic Fallback ─────────────────────────────────────────────
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


# ── Prompt Helpers ────────────────────────────────────────────────────────────
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
        first_token = line.split()[0] if line.split() else ""
        if first_token.startswith("**") or first_token[:-1].isdigit():
            continue
        if first_token.lower().rstrip(".,:") in _PROSE_STARTERS:
            continue
        return line
    return ""


# ── Ollama ────────────────────────────────────────────────────────────────────
def _ollama_warmup() -> None:
    """
    에이전트 시작 시 백그라운드에서 Ollama 모델을 미리 로드한다.
    빈 프롬프트로 generate 요청 → 토큰 생성 없이 모델만 메모리에 올림.
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
            logging.info(f"[Ollama Warmup] 모델 '{OLLAMA_MODEL}' 사전 로딩 완료 (keep_alive={_OLLAMA_KEEP_ALIVE}).")
        except Exception:
            logging.warning(f"[Ollama Warmup] 사전 로딩 실패 (Ollama 미실행 시 정상):\n{traceback.format_exc()}")

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
    prompt = _build_prompt(error_log, system_context)
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
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
                result = json.loads(resp.read())
                command = _clean_llm_output(result.get("response", ""))
                if attempt > 1:
                    logging.info(f"[Ollama] {attempt}번째 시도에서 성공")
                return command if command else "ERROR: Ollama returned empty response"

        except urllib.error.URLError as e:
            last_error = str(e)
            if attempt < _OLLAMA_MAX_RETRIES:
                wait = _OLLAMA_RETRY_BASE ** attempt  # 2s, 4s, 8s ...
                logging.warning(
                    f"[Ollama] 연결 실패 ({attempt}/{_OLLAMA_MAX_RETRIES}), "
                    f"{wait:.0f}초 후 재시도: {e}"
                )
                time.sleep(wait)
        except Exception as e:
            return f"ERROR: Ollama call failed - {e}"

    return f"ERROR: Ollama not reachable after {_OLLAMA_MAX_RETRIES} attempts - {last_error}"


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


# ── ipex_llm (Intel Arc GPU 환경 전용) ───────────────────────────────────────
mp_ctx = mp.get_context("spawn")


def _ipex_inference_worker(conn, error_log, system_context):
    import warnings
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
        os._exit(0)  # spawn 프로세스 즉시 종료 → VRAM 즉시 반환 (sys.exit은 finalizer 거침)


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


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────
def _build_response_from_meta(meta: dict, source: str) -> "AgentResponse":
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


def _ensemble_vote(candidates: list[tuple[dict, float]]) -> dict:
    """
    후보 목록에서 action_type 다수결로 최적 메타데이터를 선택한다.
    analyze_error()와 analyze_errors_batch()가 공유한다.
    """
    from collections import Counter
    action_votes = Counter(m.get("action_type", "escalate_to_human") for m, _ in candidates)
    top_action   = action_votes.most_common(1)[0][0]
    best_meta    = min(
        (m for m, _ in candidates if m.get("action_type") == top_action),
        key=lambda m: next(d for mm, d in candidates if mm is m),
        default=candidates[0][0],
    )
    return best_meta


# ── RAGEngine ─────────────────────────────────────────────────────────────────
class RAGEngine:
    def __init__(self):
        logging.info("[RAGEngine] Vector DB 연결 초기화 중...")
        client = _get_chroma_client()
        try:
            self.collection = client.get_collection(name="error_playbook_vectors")
            logging.info(f"[RAGEngine] 연결 완료. (현재 보유한 에러 지식: {self.collection.count()}개)")
        except Exception as e:
            logging.warning(f"[RAGEngine] 콜렉션 없음, 새로 생성합니다: {e}")
            self.collection = client.get_or_create_collection(name="error_playbook_vectors")
            logging.info("[RAGEngine] 빈 콜렉션 생성 완료. 추가 학습이 필요합니다.")
        _ollama_warmup()

    def _query_l1(self, log_texts: list[str]) -> dict:
        """ChromaDB 벡터 검색. 빈 컬렉션 TypeError 방어 포함."""
        try:
            return self.collection.query(query_texts=log_texts, n_results=5)
        except TypeError:
            # ChromaDB 0.5.x 버그: 빈 컬렉션 쿼리 시 int에 len() 호출해 TypeError 발생.
            n = len(log_texts)
            return {"documents": [[]] * n, "metadatas": [[]] * n, "distances": [[]] * n}

    def _l2_slow_track(self, error_log: str, best_distance: float) -> AgentResponse:
        """L1 미스 시 Ollama → ipex_llm → Rule-based → Escalation 4단계 폴백 체인."""
        logging.warning(f"[RAGEngine] 유사도 낮음 (거리: {best_distance:.4f}). Fallback 체인 시작...")
        logging.info("🔍 [Observation] 시스템 상태 사전 진단을 시작합니다...")
        system_context = gather_system_context(error_log)
        logging.info(f"📊 [진단 완료] 수집된 컨텍스트 길이: {len(system_context)}자")

        # Step 1: Ollama
        llm_result = None
        if _is_ollama_available():
            logging.info(f"[RAGEngine] Ollama({OLLAMA_MODEL}) 추론 시작...")
            llm_result = _run_ollama(error_log, system_context)
            if not llm_result.startswith("ERROR:"):
                logging.info(f"  👉 [Ollama] 명령어: {llm_result}")
                if not _reflect_on_command(llm_result, error_log, system_context):
                    logging.warning(f"[자가 반성] Ollama 명령어 거부 → 인간 에스컬레이션: {llm_result}")
                    return AgentResponse(
                        error_category="Unknown", severity="HIGH",
                        action_type=ActionType.ESCALATE_TO_HUMAN,
                        reasoning=f"자가 반성 거부 — Ollama 제안 명령어 위험 판정: {llm_result}",
                        resolution_source="L2_LLM",
                    )
                return AgentResponse(
                    error_category="LLM_Inferred", severity="CRITICAL",
                    action_type=ActionType.EXECUTE_LLM_COMMAND,
                    reasoning="Ollama 추론 성공",
                    resolution_source="L2_LLM",
                    command=llm_result,
                )
            logging.warning(f"[RAGEngine] Ollama 실패: {llm_result}")
        else:
            logging.warning("[RAGEngine] Ollama 미실행. ipex_llm으로 시도...")

        # Step 2: ipex_llm
        ipex_result = run_ipex_engine(error_log, system_context)
        if ipex_result not in ("TIMEOUT", "ERROR") and not ipex_result.startswith("ERROR:"):
            logging.info(f"  👉 [ipex_llm] 명령어: {ipex_result}")
            if not _reflect_on_command(ipex_result, error_log, system_context):
                logging.warning(f"[자가 반성] ipex 명령어 거부 → 인간 에스컬레이션: {ipex_result}")
                return AgentResponse(
                    error_category="Unknown", severity="HIGH",
                    action_type=ActionType.ESCALATE_TO_HUMAN,
                    reasoning=f"자가 반성 거부 — ipex 제안 명령어 위험 판정: {ipex_result}",
                    resolution_source="L2_LLM",
                )
            return AgentResponse(
                error_category="LLM_Inferred", severity="CRITICAL",
                action_type=ActionType.EXECUTE_LLM_COMMAND,
                reasoning="ipex_llm 추론 성공",
                resolution_source="L2_LLM",
                command=ipex_result,
            )
        logging.warning(f"[RAGEngine] ipex_llm 실패: {ipex_result}")

        # Step 3: Rule-based heuristic
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

        # Step 4: 완전 실패 → 인간 에스컬레이션
        return AgentResponse(
            error_category="Unknown", severity="HIGH",
            action_type=ActionType.ESCALATE_TO_HUMAN,
            reasoning=f"모든 Fallback 실패 (Ollama: {llm_result}, ipex: {ipex_result})",
            resolution_source="L2_LLM",
        )

    def analyze_error(self, log_text: str) -> AgentResponse:
        logging.info("[RAGEngine] 에러 로그 벡터 유사도 검색 시작...")
        start_time = time.perf_counter()
        results    = self._query_l1([log_text])
        logging.info(f"[RAGEngine] Vector DB 검색 완료 (소요시간: {time.perf_counter() - start_time:.4f}초)")

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

        candidates = [(metas[i], dists[i]) for i in range(len(dists)) if dists[i] <= _RAG_THRESHOLD]
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
        logging.info(f"[RAGEngine] 배치 Vector DB 검색 완료 ({len(log_texts)}건 / {time.perf_counter() - start:.4f}초)")

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

            candidates = [(metas[j], dists[j]) for j in range(len(dists)) if dists[j] <= _RAG_THRESHOLD]

            if not candidates:
                logging.info(f"  [배치 {i+1}/{len(log_texts)}] L1 미스 → slow track")
                responses.append(self._l2_slow_track(log_text, dists[0]))
                continue

            best_meta = _ensemble_vote(candidates)
            logging.info(
                f"  [배치 {i+1}/{len(log_texts)}] 앙상블 {len(candidates)}개 후보 "
                f"→ {best_meta.get('action_type')}"
            )
            responses.append(_build_response_from_meta(best_meta, "L1_CACHE"))

        return responses

    def learn_from_feedback(self, error_log: str, successful_command: str) -> None:
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
    test_error = "OOM killer invoked for nginx"
    print("\n--- [사전 진단 연동 테스트] ---")
    response = engine.analyze_error(test_error)
    print(f"\n최종 결과: {response.reasoning}")
