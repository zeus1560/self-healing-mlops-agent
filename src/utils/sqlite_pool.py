"""
스레드당 SQLite 연결을 재사용하는 경량 연결 풀.

매 쿼리마다 sqlite3.connect()를 새로 호출하는 오버헤드를 없애고,
WAL 모드로 읽기/쓰기 동시성을 높인다.

_creation_lock: 여러 스레드가 동시에 같은 DB 파일에 첫 연결을 맺을 때
  PRAGMA journal_mode=WAL이 충돌하지 않도록 직렬화한다.
"""
import sqlite3
import threading

_thread_local   = threading.local()
_creation_lock  = threading.Lock()


def get_conn(db_path: str) -> sqlite3.Connection:
    """db_path별로 스레드당 하나의 연결을 반환한다."""
    if not hasattr(_thread_local, "conns"):
        _thread_local.conns = {}
    if db_path not in _thread_local.conns:
        with _creation_lock:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
        _thread_local.conns[db_path] = conn
    return _thread_local.conns[db_path]
