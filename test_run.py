from profiler import MemoryProfiler, profile_performance
import time

profiler = MemoryProfiler()
profiler.print_status("1. 초기 상태 (Baseline)")

# 가상의 무거운 작업 (예: 원시 로그 10만 줄 로드)
@profile_performance
def dummy_etl_job():
    # 50MB 정도의 더미 문자열 생성
    dummy_log_data = ["ERROR: connection timeout"] * 1000000 
    time.sleep(1) # Latency 시뮬레이션
    return dummy_log_data

data = dummy_etl_job()
profiler.print_status("2. 가상 로그 로드 후")