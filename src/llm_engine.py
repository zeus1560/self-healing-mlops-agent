import os
import time
import logging
import multiprocessing as mp
import chromadb

# src.schemas 모듈이 있다고 가정 (AgentResponse, ActionType 정의)
from src.schemas import AgentResponse, ActionType

# =====================================================================
# [OS 커널 레벨 튜닝]
# 메인 프로세스가 XPU 컨텍스트를 물고 있는 상태에서 fork()가 발생하면
# 100% 데드락이 발생하므로, 완전히 깨끗한 프로세스를 생성하는 spawn 사용.
# =====================================================================
mp_ctx = mp.get_context("spawn")


def _fallback_inference_worker(conn, error_log, model_path):
    """
    [독립 프로세스 Worker]
    오직 모르는 에러(Distance > 1.2)를 만났을 때만 태어나서,
    XPU에 모델을 로드해 추론하고 결과를 반환한 뒤 즉시 자살(os._exit)하는 워커.
    """
    try:
        # 1. 지연 로딩 (메인 프로세스에서는 절대 임포트하지 않음)
        from ipex_llm.transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer

        logging.info("[FallbackWorker] XPU에 IPEX-LLM INT4 모델 적재 중...")
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForCausalLM.load_low_bit(model_path, trust_remote_code=True)
        model = model.to("xpu")

        # 2. 프롬프트 구성 (해결 커맨드만 뱉도록 유도)
        prompt = f"System: You are an MLOps AI. Analyze this error and provide a single safe shell/python command to fix it. No explanation.\nError: {error_log}\nCommand:"
        inputs = tokenizer(prompt, return_tensors="pt").to("xpu")

        # 3. 추론 (VRAM 무한 점유 방지를 위해 max_new_tokens 128 고정)
        outputs = model.generate(
            **inputs, max_new_tokens=128, temperature=0.1, do_sample=False
        )

        result_text = tokenizer.decode(outputs[0], skip_special_tokens=True)

        # 4. Pipe를 통해 메인 프로세스로 결과(추론된 커맨드) 전송
        conn.send({"status": "success", "result": result_text})

    except Exception as e:
        conn.send({"status": "error", "reason": str(e)})

    finally:
        conn.close()
        # [핵심] 파이썬 GC를 믿지 않고, C_exit() 시스템 콜로 리눅스 커널에게 VRAM 100% 즉각 회수를 강제함
        os._exit(0)


def run_fallback_engine(
    error_log: str, model_path: str = "./models/llama3-8b-ipex-int4", timeout: int = 45
) -> str:
    """
    서브 프로세스를 띄우고 라이프사이클(Timeout, Kill)을 관리하는 인터페이스
    """
    parent_conn, child_conn = mp_ctx.Pipe()
    p = mp_ctx.Process(
        target=_fallback_inference_worker, args=(child_conn, error_log, model_path)
    )
    p.start()

    if parent_conn.poll(timeout):
        response = parent_conn.recv()
        p.join()  # 정상 종료 대기

        if response["status"] == "success":
            return response["result"]
        else:
            return f"ERROR: Fallback reasoning failed - {response['reason']}"
    else:
        logging.error(
            "[FallbackEngine] Timeout! LLM 추론이 45초를 초과하여 프로세스를 강제 킬(Kill)합니다."
        )
        p.terminate()
        p.join()
        return "TIMEOUT"


# =====================================================================
# [메인 엔진 클래스]
# =====================================================================
class RAGEngine:
    """
    ChromaDB(L1 Cache) + IPEX-LLM(L2 Fallback) 하이브리드 추론 엔진.
    """

    def __init__(self):
        logging.info("[RAGEngine] Vector DB 연결 초기화 중...")
        persist_directory = os.path.join(os.getcwd(), "data", "chroma_db")
        self.chroma_client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self.chroma_client.get_collection(
            name="error_playbook_vectors"
        )
        logging.info(
            f"[RAGEngine] 연결 완료. (현재 보유한 에러 지식: {self.collection.count()}개)"
        )

    def analyze_error(self, log_text: str) -> AgentResponse:
        logging.info("[RAGEngine] 에러 로그 벡터 유사도 검색 시작...")
        start_time = time.perf_counter()

        # 1. Vector DB에 유사도 검색 (C++ ONNX 임베딩 사용)
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
        distance = results["distances"][0][0]  # L2 Distance

        logging.info(
            f"  👉 [매칭된 과거 에러] {best_match_doc[:60]}... (거리: {distance:.4f})"
        )

        # =================================================================
        # 2. [Slow Track] 모르는 에러 처리 (Fallback LLM 개입)
        # =================================================================
        if distance > 1.2:
            logging.warning(
                f"[RAGEngine] 유사도 낮음 (거리: {distance:.4f}). 지연 로딩 기반 Fallback LLM을 호출합니다..."
            )

            # 서브 프로세스로 분리된 LLM 추론 실행
            fallback_result = run_fallback_engine(log_text)

            if fallback_result in ["TIMEOUT", "ERROR"] or fallback_result.startswith(
                "ERROR:"
            ):
                return AgentResponse(
                    error_category="Unknown",
                    severity="HIGH",
                    action_type=ActionType.ESCALATE_TO_HUMAN,
                    reasoning=f"RAG 매칭 실패 및 Fallback 분석 실패: {fallback_result}",
                )
            else:
                return AgentResponse(
                    error_category="LLM_Inferred",
                    severity="CRITICAL",
                    action_type=ActionType.EXECUTE_LLM_COMMAND,
                    reasoning=fallback_result,  # LLM이 뱉은 커맨드 (이후 executor.py가 파싱)
                )

        # =================================================================
        # 3. [Fast Track] 아는 에러 처리 (Vector DB 기반 즉각 조치)
        # =================================================================
        action_str = best_match_meta.get("action", "escalate_to_human")
        try:
            action_enum = ActionType(action_str)
        except ValueError:
            action_enum = ActionType.ESCALATE_TO_HUMAN

        return AgentResponse(
            error_category=best_match_meta.get("category", "Unknown"),
            severity="HIGH",
            action_type=action_enum,
            reasoning=f"Vector DB 유사도 매칭 성공 (거리: {distance:.4f})",
        )
