"""
pii_masker — PII(개인식별정보) 마스킹 유틸리티.

에러 로그를 SQLite에 저장하기 전에 민감 정보를 치환한다.

마스킹 대상:
  - IPv4 / IPv6 주소
  - 이메일 주소
  - /home/*, /root/*, /Users/* 경로
  - AWS Access Key (AKIA 접두사)
  - GCP OAuth token (ya29.*) / GCP API Key (AIza*)
  - password=, token=, secret=, api_key= 등 key=value 패턴

성능:
  _RULES 리스트의 패턴은 모듈 임포트 시 1회만 컴파일돼
  mask() 반복 호출 비용을 최소화한다.
"""
import re

_RULES: list[tuple[re.Pattern, str]] = [
    # IPv4
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    # IPv6 (간략 패턴)
    (re.compile(r"\b([0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b"), "<IPv6>"),
    # 이메일
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b"), "<EMAIL>"),
    # 홈 디렉터리 경로
    (re.compile(r"(?:/home|/root|/Users)/[^\s/:,;\"']+"), "<PATH>"),
    # AWS Access Key (AKIA로 시작, 20자 대문자숫자)
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY>"),
    # GCP OAuth token
    (re.compile(r"\bya29\.[0-9A-Za-z_\-]{40,}\b"), "<GCP_TOKEN>"),
    # GCP API Key
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "<GCP_API_KEY>"),
    # password=, token=, secret=, key= 뒤 값 (따옴표 포함)
    (
        re.compile(
            r"(?i)(password|passwd|token|secret|api_?key|auth)\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?",
        ),
        r"\1=<REDACTED>",
    ),
]


def mask(text: str) -> str:
    """주어진 텍스트에서 PII 패턴을 마스킹한다."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
