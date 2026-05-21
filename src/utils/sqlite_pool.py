"""
sqlite_pool — 스레드당 SQLite 연결을 재사용하는 경량 연결 풀.

매 쿼리마다 sqlite3.connect()를 새로 호출하는 오버헤드를 없애고,
WAL 모드로 읽기/쓰기 동시성을 높인다.

설계 결정:
  - 연결은 threading.local()로 스레드별로 격리 → 스레드 간 연결 공유 없음.
  - _creation_lock: 여러 스레드가 동시에 같은 DB 파일에 첫 연결을 맺을 때
    PRAGMA journal_mode=WAL이 충돌하지 않도록 직렬화한다.
  - check_same_thread=False: thread-local이지만, 스레드 경계를 넘겨
    연결을 사용하는 실수를 SQLite 내부적으로도 막지 않는다.
    실질적인 보호는 thread-local 분리 자체에서 온다.
"""
import sqlite3
import threading

_thread_local  = threading.local()
_creation_lock = threading.Lock()


def get_conn(db_path: str) -> sqlite3.Connection:
    """
    db_path별로 현재 스레드에 하나의 연결을 반환한다.

    첫 호출 시에만 연결을 생성하고 WAL 모드를 설정한다.
    이후 호출은 이미 생성된 연결을 즉시 반환한다.
    """
    if not hasattr(_thread_local, "conns"):
        _thread_local.conns = {}

    if db_path not in _thread_local.conns:
        # 여러 스레드가 동시에 같은 DB에 첫 연결을 생성할 때 WAL 충돌 방지
        with _creation_lock:
            # 락 획득 후 재확인 (다른 스레드가 먼저 생성했을 수 있음)
            if db_path not in _thread_local.conns:
                conn = sqlite3.connect(db_path, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.row_factory = sqlite3.Row
                _thread_local.conns[db_path] = conn

    return _thread_local.conns[db_path]
