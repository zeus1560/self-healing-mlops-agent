import time
import hashlib
import logging
from collections import OrderedDict


class LogDebouncer:
    def __init__(self, cooldown_seconds=300, max_cache_size=1000):
        # 5분(300초)의 쿨타임 설정
        self.cooldown_seconds = cooldown_seconds
        # 메모리 누수 방지를 위해 캐시 사이즈를 제한 (LRU 방식처럼 동작)
        self.history = OrderedDict()
        self.max_cache_size = max_cache_size

    def _get_hash(self, log_text: str) -> str:
        # 에러 로그 텍스트를 고유한 짧은 해시값으로 변환
        return hashlib.md5(log_text.encode("utf-8")).hexdigest()

    def should_process(self, log_text: str) -> bool:
        current_time = time.time()
        log_hash = self._get_hash(log_text)

        # 1. 예전에 본 적 있는 에러라면 쿨타임 계산
        if log_hash in self.history:
            last_seen = self.history[log_hash]
            if current_time - last_seen < self.cooldown_seconds:
                logging.debug(
                    f"⏳ [Debounce] 쿨타임 대기 중... 중복 에러 무시: {log_text[:30]}"
                )
                return False

        # 2. 처음 보거나 쿨타임이 지난 에러라면 승인
        self.history[log_hash] = current_time

        # 3. 메모리 관리 (가장 오래된 기록 삭제)
        self.history.move_to_end(log_hash)
        if len(self.history) > self.max_cache_size:
            self.history.popitem(last=False)

        return True
