"""
profiler.py — 메모리·성능 프로파일링 유틸리티.

MemoryProfiler : 현재 프로세스의 RSS 메모리 사용량을 psutil로 측정.
profile_performance : 함수 실행 전후 메모리·시간 변화를 출력하는 데코레이터.

주의:
  모듈 임포트 시 sys.stdout을 재할당하지 않는다.
  기존 코드는 임포트만으로 모든 프로세스의 stdout 파일 디스크립터를
  교체하고 기존 핸들을 누수시키는 부작용이 있었다.
  UTF-8 출력이 필요한 경우 PYTHONIOENCODING=utf-8 환경 변수를 사용한다.
"""
import os
import time
from functools import wraps

import psutil


class MemoryProfiler:
    """현재 프로세스의 RSS 메모리 사용량을 측정한다."""

    def __init__(self):
        self.process = psutil.Process(os.getpid())

    def get_memory_mb(self) -> float:
        """현재 프로세스 RSS (MiB). UMA 구조에서는 사실상 VRAM 점유량과 직결된다."""
        return self.process.memory_info().rss / (1024 * 1024)

    def print_status(self, stage_name: str = "Status") -> None:
        mem_mb   = self.get_memory_mb()
        sys_mem  = psutil.virtual_memory()
        avail_mb = sys_mem.available / (1024 * 1024)
        print(f"[{stage_name}]")
        print(f" ├─ 현재 프로세스 점유량: {mem_mb:.2f} MB")
        print(f" ├─ 시스템 잔여 메모리  : {avail_mb:.2f} MB (사용률: {sys_mem.percent}%)")
        print("-" * 40)


def profile_performance(func):
    """함수 실행 전후 소요 시간과 메모리 증감을 출력하는 데코레이터."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        profiler   = MemoryProfiler()
        mem_before = profiler.get_memory_mb()
        start_time = time.perf_counter()

        result = func(*args, **kwargs)

        elapsed  = time.perf_counter() - start_time
        mem_diff = profiler.get_memory_mb() - mem_before
        print(f"\n[{func.__name__}]")
        print(f" ├─ 소요 시간(Latency)  : {elapsed:.4f} 초")
        print(f" ├─ 메모리 증감(Leak?): {mem_diff:+.2f} MB")
        print("-" * 40)
        return result

    return wrapper


# 하위호환성을 위한 별칭
profile_memory = profile_performance
