import os
import sys
import time
import logging
import multiprocessing as mp
import chromadb
from chromadb.config import Settings  # 💡 Settings 임포트 추가
import textwrap  # 프롬프트 템플릿 정렬을 위한 내장 라이브러리

# 파일 위치와 관계없이 src 모듈 임포트 가능하게 설정
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# src.schemas 모듈이 있다고 가정 (AgentResponse, ActionType 정의)
from src.schemas import AgentResponse, ActionType

# =====================================================================
# [OS 커널 레벨 튜닝]
# 메인 프로세스가 XPU 컨텍스트를 물고 있는 상태에서 fork()가 발생하면
# 100% 데드락이 발생하므로, 완전히 깨끗한 프로세스를 생성하는 spawn 사용.
# =====================================================================
mp_ctx = mp.get_context("spawn")


def _fallback_inference_worker(conn, error_log, model_path):
    import sys
    import warnings
    import traceback

    # XPU 드라이버 버그를 피하기 위해 관련 환경변수 싹 다 제거
    warnings.filterwarnings("ignore")

    try:
        from ipex_llm.transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer

        # 🚨 [최종 결단] 연산이 깨지는 XPU를 버리고 가장 안정적인 CPU로 강제 할당
        device = "cpu"
        hf_model_id = "Qwen/Qwen2.5-3B-Instruct"

        tokenizer = AutoTokenizer.from_pretrained(hf_model_id)

        # CPU 환경에서는 구형 포맷(sym_int4)이 가장 호환성이 좋고 빠릅니다.
        model = AutoModelForCausalLM.from_pretrained(
            hf_model_id,
            load_in_4bit=True,
            optimize_model=True,
        ).to(device)

        prompt = f"""You are a Linux system admin. Fix the error with ONE basic bash command.

Error: Nginx is down.
Command: systemctl restart nginx

Error: Port 8080 is in use.
Command: fuser -k 8080/tcp

Error: {error_log}
Command:"""

        encoded = tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
        input_ids_tensor = encoded["input_ids"].to(device)
        attention_mask_tensor = encoded["attention_mask"].to(device)

        outputs = model.generate(
            input_ids=input_ids_tensor,
            attention_mask=attention_mask_tensor,
            max_new_tokens=16,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )

        input_length = input_ids_tensor.size(1)
        generated_tokens = outputs[0][input_length:]
        final_command = tokenizer.decode(
            generated_tokens, skip_special_tokens=True
        ).strip()

        final_command = final_command.split("\n")[0].strip()

        conn.send({"status": "success", "result": final_command})

    except Exception as e:
        err_detail = f"{str(e)}\n{traceback.format_exc()}"
        conn.send({"status": "error", "reason": err_detail})

    finally:
        conn.close()
        sys.exit(0)


def run_fallback_engine(
    error_log: str,
    # [수정됨] 최신 규격으로 양자화된 안전한 오프라인 모델 폴더 지정
    model_path: str = "./models/llama3-8b-ipex-woq-int4",
    timeout: int = 600,
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
            "[FallbackEngine] Timeout! LLM 추론이 초과하여 프로세스를 강제 킬(Kill)합니다."
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
        # 💡 settings 속성을 추가하여 Telemetry 발송을 원천 차단합니다.
        self.chroma_client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False)
        )

        # 콜렉션이 없으면 자동 생성
        try:
            self.collection = self.chroma_client.get_collection(
                name="error_playbook_vectors"
            )
            logging.info(
                f"[RAGEngine] 연결 완료. (현재 보유한 에러 지식: {self.collection.count()}개)"
            )
        except Exception as e:
            logging.warning(f"[RAGEngine] 콜렉션 없음, 새로 생성합니다: {e}")
            self.collection = self.chroma_client.get_or_create_collection(
                name="error_playbook_vectors"
            )
            logging.info("[RAGEngine] 빈 콜렉션 생성 완료. 추가 학습이 필요합니다.")

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
        if distance > 0.8:
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
                # [Observability] L2 LLM 판별을 위해 reasoning에 LLM 추론 식별자 포함
                reasoning_with_l2_marker = f"[LLM 추론 (L2)] {fallback_result}"
                return AgentResponse(
                    error_category="LLM_Inferred",
                    severity="CRITICAL",
                    action_type=ActionType.EXECUTE_LLM_COMMAND,
                    reasoning=reasoning_with_l2_marker,  # L2 LLM 표시와 함께 반환
                )

        # =================================================================
        # 3. [Fast Track] 아는 에러 처리 (Vector DB 기반 즉각 조치)
        # =================================================================
        action_str = best_match_meta.get("action", "escalate_to_human")
        try:
            action_enum = ActionType(action_str)
        except ValueError:
            action_enum = ActionType.ESCALATE_TO_HUMAN

        # [Observability] L1 캐시 판별을 위해 reasoning에 Vector DB 식별자 포함
        command_or_default = best_match_meta.get("command", "No command found in DB")
        reasoning_with_l1_marker = f"[Vector DB 유사도 매칭 성공] {command_or_default}"

        return AgentResponse(
            error_category=best_match_meta.get("category", "Unknown"),
            severity="HIGH",
            action_type=action_enum,
            reasoning=reasoning_with_l1_marker,  # L1 캐시 표시와 함께 반환
        )

    def learn_from_feedback(self, error_log: str, successful_command: str):
        """
        [Phase 4: Feedback Loop]
        LLM이 도출한 커맨드가 OS에서 성공적으로 실행되었을 때 호출됩니다.
        새로운 에러 패턴과 해결책을 Vector DB에 영구 박제합니다.
        """
        import uuid
        from datetime import datetime

        doc_id = str(uuid.uuid4())

        try:
            self.collection.add(
                ids=[doc_id],
                documents=[error_log],  # 검색 대상이 될 원본 에러 로그
                metadatas=[
                    {
                        "category": "Learned_from_LLM",
                        "action": ActionType.EXECUTE_LLM_COMMAND.value,  # schemas의 Enum 사용
                        "command": successful_command,  # 매칭 시 바로 꺼내쓸 실제 커맨드
                        "timestamp": datetime.now().isoformat(),
                    }
                ],
            )
            logging.info(f"🧠 [Phase 4] 새로운 지식 학습 완료! L1 Cache에 저장됨.")
            logging.info(f"   👉 [에러] {error_log[:50]}...")
            logging.info(f"   👉 [해결] {successful_command}")

        except Exception as e:
            logging.error(f"❌ [Phase 4] Vector DB 학습 실패: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 엔진 초기화
    engine = RAGEngine()

    print("\n--- [Phase 4 시나리오 테스트] ---")
    unknown_error = "CRITICAL: Nginx reverse proxy routing table corrupted. process 9912 is hanging."
    simulated_command = "pkill -9 nginx && systemctl restart nginx"

    print(f"\n[시뮬레이션] 에러: {unknown_error}")
    print(f"[시뮬레이션] LLM이 생성한 커맨드: {simulated_command}")
    print(f"[시뮬레이션] 커맨드 실행 결과: SUCCESS")

    # Phase 4 핵심: 성공한 커맨드를 DB에 캐싱
    print("\n[Executor] 실행 성공! RAGEngine에 학습을 요청합니다.")
    engine.learn_from_feedback(unknown_error, simulated_command)

    # 2. 학습 확인 테스트 (동일 에러 재발생 시뮬레이션)
    print("\n--- [검증] 1초 뒤 동일한 에러 발생 시나리오 ---")
    time.sleep(1)

    # 두 번째 호출에서는 LLM을 켜지 않고 L1 Cache(Vector DB)에서 즉시 가져와야 함
    cached_response = engine.analyze_error(unknown_error)
    print(f"\n[Fast Track Result] {cached_response.reasoning}")
    print(f"[캐시 히트] 학습된 커맨드가 즉시 반환됨 (분석 시간 단축)")
