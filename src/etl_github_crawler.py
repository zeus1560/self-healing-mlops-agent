import os
import requests
import logging
import time
from dotenv import load_dotenv  # 👈 추가: dotenv 라이브러리 임포트
from etl_ingest import load_data_to_pg

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# 👈 추가: .env 파일에 있는 변수들을 OS 환경변수로 불러옴
load_dotenv()

# 👈 수정: 하드코딩 대신 환경변수에서 읽어옴
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not GITHUB_TOKEN:
    raise ValueError("🚨 .env 파일에 GITHUB_TOKEN이 설정되지 않았습니다!")

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "Authorization": f"token {GITHUB_TOKEN}",
}

# 우리가 계획한 클래스 불균형 방지용 '타겟 쿼리' 리스트
# 각 카테고리별로 정확히 50개(per_page=50)만 가져옵니다.
TARGET_QUERIES = [
    {
        "name": "PyTorch OOM",
        "query": 'repo:pytorch/pytorch "CUDA out of memory" is:closed',
        "error_category": "Out_Of_Memory",
        "severity": "CRITICAL",
        "action_type": "clear_memory",
        "target_process": None,
        "reasoning": "PyTorch 공식 리포지토리에서 수집된 VRAM 부족 에러. 시스템 캐시 초기화 필요.",
    },
    {
        "name": "PostgreSQL Timeout",
        "query": 'repo:psycopg/psycopg2 "server closed the connection" OR "timeout" is:closed',
        "error_category": "Network_Timeout",
        "severity": "HIGH",
        "action_type": "restart_service",
        "target_process": "postgres_pool",
        "reasoning": "psycopg2 공식 리포지토리에서 수집된 DB 연결 지연 에러. 커넥션 풀 재시작 필요.",
    },
    {
        "name": "Django Config Error",
        "query": 'repo:django/django "ImproperlyConfigured" is:closed',
        "error_category": "Configuration_Error",
        "severity": "MEDIUM",
        "action_type": "escalate_to_human",
        "target_process": None,
        "reasoning": "Django 공식 리포지토리에서 수집된 환경설정 누락 에러. 시스템 자동 복구 불가, 관리자 개입 필요.",
    },
]


def fetch_github_issues(limit_per_query=50) -> list[tuple]:
    """GitHub API를 호출하여 타겟팅된 에러 이슈를 수집하고 튜플 형태로 변환합니다."""
    all_extracted_data = []

    for target in TARGET_QUERIES:
        logging.info(f"🔍 [{target['name']}] 데이터 수집 시작...")
        url = f"https://api.github.com/search/issues?q={target['query']}&per_page={limit_per_query}"

        response = requests.get(url, headers=HEADERS)

        if response.status_code != 200:
            logging.error(f"API 호출 실패 ({response.status_code}): {response.text}")
            continue

        items = response.json().get("items", [])
        valid_count = 0

        for item in items:
            body = item.get("body")
            # 본문이 비어있지 않은 이슈만 필터링
            if not body:
                continue

            # 에러 로그 본문이 너무 길면 DB와 임베딩 모델에 과부하를 주므로 1000자로 자름
            truncated_body = body[:1000]

            # DB 스키마에 맞는 튜플 생성 (log_text, error_category, severity, action_type, target_process, reasoning)
            row = (
                truncated_body,
                target["error_category"],
                target["severity"],
                target["action_type"],
                target["target_process"],
                target["reasoning"],
            )
            all_extracted_data.append(row)
            valid_count += 1

        logging.info(f"✅ [{target['name']}] 유효한 데이터 {valid_count}건 추출 완료.")
        time.sleep(2)  # API Rate Limit 보호를 위한 딜레이

    return all_extracted_data


if __name__ == "__main__":
    logging.info("--- GitHub 타겟팅 크롤러 시작 ---")

    # 1. Extract & Transform (수집 및 스키마 변환)
    crawled_data = fetch_github_issues(limit_per_query=50)

    if crawled_data:
        # 2. Load (PostgreSQL에 적재) - 이전 스크립트 모듈 재사용
        logging.info(f"총 {len(crawled_data)}건의 데이터를 DB에 적재합니다.")
        load_data_to_pg(crawled_data)
    else:
        logging.warning("수집된 데이터가 없습니다.")
