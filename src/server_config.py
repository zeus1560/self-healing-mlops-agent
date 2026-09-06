"""
server_config — 서버 목록 설정 로더 (2026-09-06 추가).

지금은 단일 서버만 운영하지만, Phase 3 k8s 연동 전에 서버 접속정보/로그 경로/
타겟앱 URL을 리스트화 대비 구조로 미리 정리해둔다(9/3 /grill-me 세션 결정).
서버가 여러 대가 돼도 config/servers.yaml에 항목만 추가하면 되고, 이 모듈의
API는 안 바뀐다.

에러 카테고리 → 복구 액션 매핑은 여기 안 두고 코드(src/llm_engine.py 등)에
그대로 유지한다 — 그건 서버 다중화와는 다른 축의 관심사.
"""
import os

import yaml

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "servers.yaml",
)
_CONFIG_PATH = os.getenv("SERVERS_CONFIG_PATH", _DEFAULT_CONFIG_PATH)

# config/servers.yaml이 없거나 비어 있을 때 사용하는 안전한 기본값.
# 기존(config화 이전) 하드코딩 값과 동일하게 맞춰 동작 회귀를 방지한다.
_FALLBACK_SERVER: dict = {
    "name":           "default",
    "log_path":       "./data/realtime_system.log",
    "target_app_url": "http://localhost:9000",
    "exec_method":    "systemd",
}


def load_servers() -> list[dict]:
    """config/servers.yaml을 읽어 서버 설정 리스트를 반환한다. 파일 없으면 빈 리스트."""
    if not os.path.exists(_CONFIG_PATH):
        return []
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return []
    return data.get("servers") or []


def get_server(name: str | None = None) -> dict:
    """
    이름으로 서버 설정을 찾는다. name이 None이면 목록의 첫 번째(기본 서버) 반환.

    설정 파일이 없거나 서버가 하나도 없으면 _FALLBACK_SERVER를 반환해
    config화 이전과 동일하게 동작한다.
    """
    servers = load_servers()
    if not servers:
        return dict(_FALLBACK_SERVER)
    if name is None:
        return servers[0]
    for s in servers:
        if s.get("name") == name:
            return s
    raise KeyError(
        f"서버 설정을 찾을 수 없음: {name!r} "
        f"(등록된 서버: {[s.get('name') for s in servers]})"
    )
