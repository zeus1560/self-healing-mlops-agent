import logging
import os
import time
import traceback

import requests
from dotenv import load_dotenv

from src.etl_ingest import load_data_to_pg

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ValueError("🚨 .env 파일에 GITHUB_TOKEN이 설정되지 않았습니다!")

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "Authorization": f"token {GITHUB_TOKEN}",
}

TARGET_QUERIES = [
    # ── 기존 3개 ───────────────────────────────────────────────────────────
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
    # ── 신규 7개 ───────────────────────────────────────────────────────────
    {
        "name": "Redis DB Connection",
        "query": 'repo:redis/redis "Connection refused" OR "ECONNREFUSED" is:closed',
        "error_category": "DB_Connection",
        "severity": "HIGH",
        "action_type": "restart_service",
        "target_process": "redis",
        "reasoning": "Redis 공식 리포지토리에서 수집된 DB 연결 거부 에러. Redis 서비스 재시작 필요.",
    },
    {
        "name": "Ansible Permission Denied",
        "query": 'repo:ansible/ansible "Permission denied" is:closed',
        "error_category": "Permission_Denied",
        "severity": "HIGH",
        "action_type": "escalate_to_human",
        "target_process": None,
        "reasoning": "Ansible 리포지토리에서 수집된 파일 권한 에러. OS 권한 문제로 자동 복구 불가, 관리자 개입 필요.",
    },
    {
        "name": "Docker Disk Full",
        "query": 'repo:docker/compose "no space left on device" is:closed',
        "error_category": "Disk_Full",
        "severity": "CRITICAL",
        "action_type": "execute_llm_command",
        "target_process": None,
        "reasoning": "Docker 리포지토리에서 수집된 디스크 용량 부족 에러. 불필요한 이미지/컨테이너 정리 필요.",
    },
    {
        "name": "Kubernetes Process Crash",
        "query": 'repo:kubernetes/kubernetes "CrashLoopBackOff" OR "OOMKilled" is:closed',
        "error_category": "Process_Crash",
        "severity": "CRITICAL",
        "action_type": "restart_service",
        "target_process": "pod",
        "reasoning": "Kubernetes 리포지토리에서 수집된 컨테이너 크래시 에러. 파드 재시작 및 리소스 리밋 점검 필요.",
    },
    {
        "name": "Nginx Port Conflict",
        "query": 'repo:nginx/nginx "Address already in use" OR "bind() failed" is:closed',
        "error_category": "Port_Conflict",
        "severity": "HIGH",
        "action_type": "restart_service",
        "target_process": "nginx",
        "reasoning": "Nginx 리포지토리에서 수집된 포트 충돌 에러. 기존 프로세스 종료 후 서비스 재시작 필요.",
    },
    {
        "name": "Vault Auth Error",
        "query": 'repo:hashicorp/vault "permission denied" OR "authentication failed" is:closed',
        "error_category": "Auth_Error",
        "severity": "HIGH",
        "action_type": "escalate_to_human",
        "target_process": "vault",
        "reasoning": "HashiCorp Vault 리포지토리에서 수집된 인증 실패 에러. 토큰 만료 또는 정책 문제로 관리자 확인 필요.",
    },
    {
        "name": "Go Memory Leak",
        "query": 'repo:golang/go "runtime: out of memory" OR "memory leak" is:closed',
        "error_category": "Memory_Leak",
        "severity": "HIGH",
        "action_type": "kill_process",
        "target_process": None,
        "reasoning": "Go 공식 리포지토리에서 수집된 메모리 누수 에러. 해당 프로세스 재시작 및 힙 덤프 분석 필요.",
    },
]

_RATE_LIMIT_SLEEP = 60  # 429 응답 시 대기 시간(초)


def _get_with_retry(url: str, max_retries: int = 3) -> requests.Response | None:
    """429 Rate Limit 시 지수 백오프 후 재시도."""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
        except Exception:
            logging.error(f"HTTP 요청 실패 (시도 {attempt + 1}/{max_retries}):\n{traceback.format_exc()}")
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            continue

        if response.status_code == 200:
            return response
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", _RATE_LIMIT_SLEEP))
            logging.warning(f"Rate Limit 도달. {retry_after}초 대기 후 재시도...")
            time.sleep(retry_after)
            continue
        logging.error(f"API 호출 실패 (HTTP {response.status_code}): {response.text[:200]}")
        return None

    logging.error(f"최대 재시도 횟수 초과: {url}")
    return None


def fetch_github_issues(limit_per_query: int = 50) -> list[tuple]:
    all_extracted_data: list[tuple] = []

    for target in TARGET_QUERIES:
        logging.info(f"[{target['name']}] 데이터 수집 시작...")
        url = (
            f"https://api.github.com/search/issues"
            f"?q={target['query']}&per_page={limit_per_query}"
        )

        response = _get_with_retry(url)
        if response is None:
            continue

        try:
            items = response.json().get("items", [])
        except Exception:
            logging.error(
                f"[{target['name']}] JSON 파싱 실패:\n{traceback.format_exc()}"
            )
            continue

        seen_texts: set[str] = set()
        valid_count = 0

        for item in items:
            body = item.get("body")
            if not body or not body.strip():
                continue

            truncated_body = body[:1000].strip()
            if truncated_body in seen_texts:
                continue
            seen_texts.add(truncated_body)

            all_extracted_data.append((
                truncated_body,
                target["error_category"],
                target["severity"],
                target["action_type"],
                target["target_process"],
                target["reasoning"],
            ))
            valid_count += 1

        logging.info(f"[{target['name']}] 유효 고유 데이터 {valid_count}건 추출 완료.")
        time.sleep(2)  # API Rate Limit 보호

    return all_extracted_data


if __name__ == "__main__":
    logging.info("--- GitHub 타겟팅 크롤러 시작 ---")
    crawled_data = fetch_github_issues(limit_per_query=50)

    if crawled_data:
        logging.info(f"총 {len(crawled_data)}건의 데이터를 DB에 적재합니다.")
        load_data_to_pg(crawled_data)
    else:
        logging.warning("수집된 데이터가 없습니다.")
