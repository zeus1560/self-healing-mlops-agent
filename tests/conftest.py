"""
pytest 공통 픽스처.

각 테스트 클래스가 직접 tempfile을 관리하는 대신 여기서 제공하는 픽스처를 사용하면
tearDown 누락으로 인한 파일 잔재 문제가 없어진다.
"""
import os
import pytest


@pytest.fixture
def temp_db(tmp_path):
    """테스트 전용 SQLite DB 경로 (테스트 종료 시 pytest가 자동 삭제)."""
    return str(tmp_path / "test.db")


@pytest.fixture
def temp_log(tmp_path):
    """내용이 있는 임시 로그 파일 경로."""
    path = str(tmp_path / "test.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("=== test log ===\n")
    return path


@pytest.fixture(autouse=True)
def isolate_approval_store(tmp_path):
    """
    모든 테스트에서 approval_store가 실제 DB 대신 임시 DB를 사용하도록 격리.
    autouse=True 이므로 각 테스트가 신경 쓰지 않아도 자동 적용된다.
    """
    import src.approval_store as store
    orig = store._DB_PATH
    store._DB_PATH = str(tmp_path / "approval_test.db")
    store.init_table()
    yield
    store._DB_PATH = orig
