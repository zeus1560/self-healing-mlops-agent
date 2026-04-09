import os
import psutil
import time
from functools import wraps
import sys

# UTF-8 인코딩 설정
if sys.stdout.encoding != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)


class MemoryProfiler:
    def __init__(self):
        self.process = psutil.Process(os.getpid())

    def get_memory_mb(self):
        """현재 프로세스가 점유 중인 메모리 (UMA 구조이므로 사실상 VRAM 점유량과 직결됨)"""
        return self.process.memory_info().rss / (1024 * 1024)

    def print_status(self, stage_name="Status"):
        mem_mb = self.get_memory_mb()
        sys_mem = psutil.virtual_memory()
        avail_mb = sys_mem.available / (1024 * 1024)
        percent = sys_mem.percent

        print(f"[{stage_name}]")
        print(f" ├─ 현재 프로세스 점유량: {mem_mb:.2f} MB")
        print(f" ├─ 시스템 잔여 메모리  : {avail_mb:.2f} MB (사용률: {percent}%)")
        print("-" * 40)


# 실무에서 편하게 쓰기 위한 데코레이터 (함수 실행 전후의 메모리/시간 비교)
def profile_performance(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        profiler = MemoryProfiler()
        print(f"\n🚀 [Start] {func.__name__} 실행 시작...")
        mem_before = profiler.get_memory_mb()
        start_time = time.perf_counter()

        result = func(*args, **kwargs)

        end_time = time.perf_counter()
        mem_after = profiler.get_memory_mb()

        print(f"🏁 [End] {func.__name__} 실행 완료!")
        print(f" ├─ 소요 시간(Latency)  : {end_time - start_time:.4f} 초")
        print(f" ├─ 메모리 증감(Leak?): {mem_after - mem_before:+.2f} MB")
        print("-" * 40)
        return result

    return wrapper


# 하위호환성을 위한 별칭
profile_memory = profile_performance
