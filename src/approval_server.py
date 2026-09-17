"""
Human-in-the-Loop 승인 서버.

흐름:
  1. Slack → GET /pending/{token}  : 명령어 확인 페이지 (승인/거절 버튼 포함)
  2. 관리자 버튼 클릭 → POST /approve/{token} 또는 /reject/{token}
     (2026-09-17까지는 GET이었다 — 상태를 바꾸는 요청이 GET이면 메신저의 링크
     미리보기 크롤러나 백신 링크 스캐너가 그 URL을 자동으로 fetch하는 것만으로도
     사람이 실제로 클릭하지 않은 승인/거절이 기록될 수 있었다. 버튼을 눌러야
     제출되는 <form method="post">로 바꿔 이 문제를 막는다.)
  3. executor.py 데몬 모드 폴링 → approval_store.get_status(token)

승인자 식별:
  이 서버엔 로그인/인증 체계가 없어 실명을 알 방법이 없다 — 최소한의 신원
  신호로 요청 클라이언트 IP를 "web:{ip}"로 기록한다(텔레그램 경로는
  telegram_bot.py에서 실제 사용자 ID/이름을 기록 — 그쪽이 훨씬 신뢰도 높음).

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

from fastapi import FastAPI, Request
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
    # <a href> 링크였던 것을 <form method="post">로 변경 — 링크 미리보기 크롤러/
    # 백신 스캐너가 GET으로 자동 fetch해도 상태가 안 바뀌게 하기 위함.
    return f"""
<html><body style="{_STYLE}">
  <h2 style="color:#2980b9">🔐 명령어 실행 승인 요청</h2>
  <p style="color:#888">이 요청은 <b>{EXPIRY_MINUTES}분</b> 후 자동 만료됩니다.</p>
  <h4>실행될 명령어</h4>
  <pre style="background:#f4f4f4;padding:12px;border-radius:4px">{safe_cmd}</pre>
  <h4>트리거된 에러 로그</h4>
  <pre style="background:#fff3cd;padding:12px;border-radius:4px;font-size:0.85em">{safe_log}</pre>
  <div style="margin-top:32px;display:flex;gap:16px">
    <form method="post" action="{approve_url}" style="flex:1">
      <button type="submit" style="width:100%;padding:14px;background:#2ecc71;color:#fff;
        border:none;border-radius:6px;font-size:1.1em;cursor:pointer">✅ 승인</button>
    </form>
    <form method="post" action="{reject_url}" style="flex:1">
      <button type="submit" style="width:100%;padding:14px;background:#e74c3c;color:#fff;
        border:none;border-radius:6px;font-size:1.1em;cursor:pointer">🚫 거절</button>
    </form>
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


def _client_identity(request: Request) -> str:
    """요청 클라이언트의 최소 신원 신호 — 로그인 체계가 없어 IP 이상은 알 수 없다."""
    return f"web:{request.client.host}" if request.client else "web:unknown"


@app.post("/approve/{token}", response_class=HTMLResponse)
def approve(token: str, request: Request):
    err = _check_status(token)
    if err:
        return err
    approval_store.set_decision(token, "approved", _client_identity(request))
    return HTMLResponse(_OK_HTML)


@app.post("/reject/{token}", response_class=HTMLResponse)
def reject(token: str, request: Request):
    err = _check_status(token)
    if err:
        return err
    approval_store.set_decision(token, "rejected", _client_identity(request))
    return HTMLResponse(_REJECT_HTML)


@app.get("/health")
def health():
    return {"status": "ok"}
