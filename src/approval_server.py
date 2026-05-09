"""
Human-in-the-Loop 승인 서버.

흐름:
  1. Slack → GET /pending/{token}  : 명령어 확인 페이지 (승인/거절 버튼 포함)
  2. 관리자 버튼 클릭 → GET /approve/{token} 또는 /reject/{token}
  3. executor.py 데몬 모드 폴링 → approval_store.get_status(token)

보안:
  - 토큰은 secrets.token_urlsafe(32) (256비트 엔트로피)
  - EXPIRY_MINUTES (기본 10분) 이후 토큰 자동 만료
  - /pending/{token} 확인 페이지로 맹목적 승인 방지

실행:
    uvicorn src.approval_server:app --host 0.0.0.0 --port 8080
환경 변수:
    APPROVAL_BASE_URL  외부에서 접근 가능한 베이스 URL (예: https://agent.example.com)
"""
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from src import approval_store
from src.approval_store import EXPIRY_MINUTES

approval_store.init_table()

app = FastAPI(title="MLOps Agent Approval Server", docs_url=None, redoc_url=None)

_STYLE = "font-family:sans-serif;max-width:640px;margin:60px auto;padding:0 20px"


def _pending_html(token: str, command: str, error_log: str) -> str:
    base_url = os.getenv("APPROVAL_BASE_URL", "http://localhost:8080")
    approve_url = f"{base_url}/approve/{token}"
    reject_url  = f"{base_url}/reject/{token}"
    safe_cmd = command.replace("<", "&lt;").replace(">", "&gt;")
    safe_log = (error_log or "")[:300].replace("<", "&lt;").replace(">", "&gt;")
    return f"""
<html><body style="{_STYLE}">
  <h2 style="color:#2980b9">🔐 명령어 실행 승인 요청</h2>
  <p style="color:#888">이 요청은 <b>{EXPIRY_MINUTES}분</b> 후 자동 만료됩니다.</p>
  <h4>실행될 명령어</h4>
  <pre style="background:#f4f4f4;padding:12px;border-radius:4px">{safe_cmd}</pre>
  <h4>트리거된 에러 로그</h4>
  <pre style="background:#fff3cd;padding:12px;border-radius:4px;font-size:0.85em">{safe_log}</pre>
  <div style="margin-top:32px;display:flex;gap:16px">
    <a href="{approve_url}" style="flex:1;text-align:center;padding:14px;background:#2ecc71;
       color:#fff;text-decoration:none;border-radius:6px;font-size:1.1em">✅ 승인</a>
    <a href="{reject_url}" style="flex:1;text-align:center;padding:14px;background:#e74c3c;
       color:#fff;text-decoration:none;border-radius:6px;font-size:1.1em">🚫 거절</a>
  </div>
</body></html>
"""


_OK_HTML = f"""
<html><body style="{_STYLE}">
<h2 style="color:#2ecc71">✅ 승인 완료</h2>
<p>명령어가 실행됩니다. 이 창을 닫아도 됩니다.</p>
</body></html>
"""

_REJECT_HTML = f"""
<html><body style="{_STYLE}">
<h2 style="color:#e74c3c">🚫 거절됨</h2>
<p>명령어 실행이 취소되었습니다. 이 창을 닫아도 됩니다.</p>
</body></html>
"""

_GONE_HTML = f"""
<html><body style="{_STYLE}">
<h2 style="color:#e67e22">⚠️ 이미 처리된 요청</h2>
<p>이 요청은 이미 승인 또는 거절되었습니다.</p>
</body></html>
"""

_EXPIRED_HTML = f"""
<html><body style="{_STYLE}">
<h2 style="color:#e67e22">⏰ 토큰 만료</h2>
<p>승인 유효 시간({EXPIRY_MINUTES}분)이 초과되었습니다. 새 에러 발생 시 새 요청이 전송됩니다.</p>
</body></html>
"""

_NOT_FOUND_HTML = f"""
<html><body style="{_STYLE}">
<h2 style="color:#e67e22">⚠️ 요청을 찾을 수 없습니다</h2>
<p>토큰이 유효하지 않습니다.</p>
</body></html>
"""


def _check_status(token: str):
    """공통 상태 검사. 문제가 있으면 HTMLResponse 반환, 없으면 None."""
    status = approval_store.get_status(token)
    if status is None:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    if status == "expired":
        return HTMLResponse(_EXPIRED_HTML, status_code=410)
    if status != "pending":
        return HTMLResponse(_GONE_HTML, status_code=409)
    return None


@app.get("/pending/{token}", response_class=HTMLResponse)
def pending(token: str):
    """명령어 확인 페이지 — Slack에서 이 URL로 먼저 진입하여 내용을 확인 후 승인/거절."""
    err = _check_status(token)
    if err:
        return err
    req = approval_store.get_request(token)
    if req is None:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    return HTMLResponse(_pending_html(token, req["command"], req.get("error_log", "")))


@app.get("/approve/{token}", response_class=HTMLResponse)
def approve(token: str):
    err = _check_status(token)
    if err:
        return err
    approval_store.set_decision(token, "approved")
    return HTMLResponse(_OK_HTML)


@app.get("/reject/{token}", response_class=HTMLResponse)
def reject(token: str):
    err = _check_status(token)
    if err:
        return err
    approval_store.set_decision(token, "rejected")
    return HTMLResponse(_REJECT_HTML)


@app.get("/health")
def health():
    return {"status": "ok"}
