"""
Human-in-the-Loop 승인 서버.

Slack 버튼 → GET /approve/{token} 또는 /reject/{token}
executor.py 데몬 모드가 폴링 → approval_store.get_status(token)

실행:
    uvicorn src.approval_server:app --host 0.0.0.0 --port 8080
환경 변수:
    APPROVAL_BASE_URL  외부에서 접근 가능한 베이스 URL (예: https://agent.example.com)
                       Slack 버튼 URL 생성에 사용. 미설정 시 http://localhost:8080
"""
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from src import approval_store

approval_store.init_table()

app = FastAPI(title="MLOps Agent Approval Server", docs_url=None, redoc_url=None)

_OK_HTML = """
<html><body style="font-family:sans-serif;text-align:center;padding:60px">
<h2 style="color:#2ecc71">✅ 승인 완료</h2>
<p>명령어가 실행됩니다. 이 창을 닫아도 됩니다.</p>
</body></html>
"""

_REJECT_HTML = """
<html><body style="font-family:sans-serif;text-align:center;padding:60px">
<h2 style="color:#e74c3c">🚫 거절됨</h2>
<p>명령어 실행이 취소되었습니다. 이 창을 닫아도 됩니다.</p>
</body></html>
"""

_GONE_HTML = """
<html><body style="font-family:sans-serif;text-align:center;padding:60px">
<h2 style="color:#e67e22">⚠️ 이미 처리된 요청</h2>
<p>이 요청은 이미 승인 또는 거절되었습니다.</p>
</body></html>
"""

_NOT_FOUND_HTML = """
<html><body style="font-family:sans-serif;text-align:center;padding:60px">
<h2 style="color:#e67e22">⚠️ 요청을 찾을 수 없습니다</h2>
<p>토큰이 만료되었거나 유효하지 않습니다.</p>
</body></html>
"""


@app.get("/approve/{token}", response_class=HTMLResponse)
def approve(token: str):
    status = approval_store.get_status(token)
    if status is None:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    if status != "pending":
        return HTMLResponse(_GONE_HTML, status_code=409)
    approval_store.set_decision(token, "approved")
    return HTMLResponse(_OK_HTML)


@app.get("/reject/{token}", response_class=HTMLResponse)
def reject(token: str):
    status = approval_store.get_status(token)
    if status is None:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    if status != "pending":
        return HTMLResponse(_GONE_HTML, status_code=409)
    approval_store.set_decision(token, "rejected")
    return HTMLResponse(_REJECT_HTML)


@app.get("/health")
def health():
    return {"status": "ok"}
