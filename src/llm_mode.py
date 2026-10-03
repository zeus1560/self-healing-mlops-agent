"""
L2 LLM 모드(LLM_PROVIDER) — 회사 정책에 맞춰 둘 중 하나를 고른다 (2026-10-04, §6 B2).

  ollama (로컬 모드, 기본값): L2 분석용 데이터가 서버 밖으로 나가지 않는다. 어떤 경우에도
                              Groq를 호출하지 않는다(키가 있어도, Ollama가 죽어도).
  groq   (클라우드 모드)    : 명시적으로 켠 경우만. 에러 로그·시스템 상태가 api.groq.com으로
                              전송된다. Groq 실패 시 로컬 Ollama로 폴백(외부→로컬 방향이라 허용).

이전엔 GROQ_API_KEY만 있으면 자동으로 Groq를 1순위로 썼다 — 셀프호스팅 포지셔닝과 충돌.
미설정·오타는 전부 ollama로 처리한다: 설정 실수가 외부 전송으로 이어지지 않게 하기 위함.

llm_engine(chromadb 등 무거운 의존성)과 autonomy_store(로컬 모드 auto 승급 차단)가 함께
쓰므로 의존성 없는 별도 모듈로 둔다. 값은 프로세스 시작 시 한 번 읽는다 — 모드를 바꾸려면
.env 수정 후 서비스를 재시작한다.
"""
import os

from dotenv import load_dotenv

load_dotenv()

VALID_LLM_PROVIDERS = ("ollama", "groq")
LLM_PROVIDER_RAW    = os.getenv("LLM_PROVIDER", "")

# 로컬 모드에서 auto로 승급할 수 없는 카테고리 — 로컬 모델이 만든 L2 자유형식 명령.
# 2026-10-04 실측에서 로컬 모델(qwen2.5:0.5b/3b)의 대상 일치 조치 비율은 0~2%였고 통과분
# 대부분이 프롬프트 예시 복사였다 — 사람 승인 없이 실행되면 에러와 무관한 명령이 돈다.
LOCAL_MODE_NO_AUTO_CATEGORIES = frozenset({"LLM_Inferred"})


def resolve_llm_provider(raw: str) -> str:
    """LLM_PROVIDER 원본 값을 유효한 provider로 정규화한다(미설정/잘못된 값 → ollama)."""
    value = raw.strip().lower()
    return value if value in VALID_LLM_PROVIDERS else "ollama"


LLM_PROVIDER = resolve_llm_provider(LLM_PROVIDER_RAW)


def is_local_mode() -> bool:
    return LLM_PROVIDER != "groq"
