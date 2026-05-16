import hashlib
import re
import time
from collections import OrderedDict

# 에러 로그에서 가변 값을 플레이스홀더로 치환하는 규칙.
# 순서 중요: IPv4를 먼저 처리해야 숫자 규칙에 IP가 분해되지 않는다.
_NORM_RULES = [
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b'), '<IP>'),  # IPv4[:port]
    (re.compile(r'\b[0-9a-f]{8,}\b', re.IGNORECASE), '<HEX>'),       # 메모리 주소·해시
    (re.compile(r'\b\d+\b'), '<N>'),                                   # 정수 (PID, 포트 등)
    (re.compile(r'\s+'), ' '),                                         # 공백 정규화
]


class LogDebouncer:
    """
    유사 에러 로그가 cooldown 시간 내에 반복되면 무시합니다.

    기존: 전체 텍스트 MD5 → 완전히 동일한 메시지만 중복 처리
    개선: 첫 줄 정규화(숫자·IP 제거) 후 MD5 → PID·IP가 달라도 같은 에러로 묶음

    예) "OOM killed pid 1234" == "OOM killed pid 5678" → 동일 버킷
        "Connection refused to 10.0.0.1" == "Connection refused to 192.168.1.1" → 동일 버킷

    LRU 방식으로 캐시 크기를 max_cache_size로 제한합니다.
    """

    def __init__(self, cooldown_seconds: int = 30, max_cache_size: int = 1000):
        self.cooldown_seconds = cooldown_seconds
        self.max_cache_size   = max_cache_size
        self.history: OrderedDict = OrderedDict()

    @staticmethod
    def _normalize(text: str) -> str:
        """
        첫 줄만 취하고 가변 값(숫자, IP, 해시)을 플레이스홀더로 치환한다.
        대소문자 무시로 같은 에러 패턴을 하나의 키로 수렴시킨다.
        """
        line = text.splitlines()[0].strip().lower() if text else ""
        for pattern, repl in _NORM_RULES:
            line = pattern.sub(repl, line)
        return line.strip()

    def _key(self, text: str) -> str:
        return hashlib.md5(self._normalize(text).encode("utf-8")).hexdigest()

    def should_process(self, log_text: str) -> bool:
        """True면 처리, False면 쿨타임 중 유사 중복이므로 무시."""
        now = time.time()
        key = self._key(log_text)

        if key in self.history and now - self.history[key] < self.cooldown_seconds:
            return False

        self.history[key] = now
        self.history.move_to_end(key)
        if len(self.history) > self.max_cache_size:
            self.history.popitem(last=False)
        return True

    def is_new_error(self, log_text: str) -> bool:
        """log_monitor.LogMonitor 호환 인터페이스."""
        return self.should_process(log_text)
