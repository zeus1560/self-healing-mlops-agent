import hashlib
import time
from collections import OrderedDict


class LogDebouncer:
    """
    동일 에러 로그가 cooldown 시간 내에 반복되면 무시합니다.
    LRU 방식으로 캐시 크기를 제한합니다.
    """

    def __init__(self, cooldown_seconds: int = 30, max_cache_size: int = 1000):
        self.cooldown_seconds = cooldown_seconds
        self.history: OrderedDict = OrderedDict()
        self.max_cache_size = max_cache_size

    def _hash(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def should_process(self, log_text: str) -> bool:
        """True면 처리, False면 쿨타임 중 중복이므로 무시."""
        now = time.time()
        key = self._hash(log_text)

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
