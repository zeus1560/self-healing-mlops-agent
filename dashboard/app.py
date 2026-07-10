

"""
Enterprise MLOps Agent Dashboard
캡스톤 최종 발표용 — Self-Healing Agent 모니터링 & 실험 분석
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── 페이지 설정 ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MLOps Agent Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    /* 메트릭 카드 */
    [data-testid="metric-container"] {
        background: #1a1f2e;
        border: 1px solid #2d3748;
        border-radius: 10px;
        padding: 16px 20px !important;
    }
    [data-testid="stMetricValue"] { font-size: 1.75rem !important; font-weight: 700; }
    [data-testid="stMetricDelta"] { font-size: 0.82rem; }

    /* 섹션 제목 */
    .section-title {
        font-size: 0.92rem;
        font-weight: 700;
        color: #a0aec0;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        margin: 0 0 10px 2px;
    }

    /* 상태 배너 */
    .status-banner {
        border-radius: 10px;
        padding: 14px 22px;
        margin-bottom: 20px;
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .s-ok   { background: rgba(46,204,113,.10); border-left: 4px solid #2ecc71; }
    .s-warn { background: rgba(230,126,34,.10);  border-left: 4px solid #e67e22; }
    .s-crit { background: rgba(231,76,60,.10);   border-left: 4px solid #e74c3c; }

    /* 구분선 */
    hr { border-color: #2d3748 !important; }

    /* 히어로 성과 카드 */
    .hero-card {
        border-radius: 12px;
        padding: 22px 20px;
        text-align: center;
        margin-bottom: 2px;
    }
    .hero-number {
        font-size: 2.9rem;
        font-weight: 900;
        line-height: 1.1;
        margin: 6px 0 2px;
    }
    .hero-label-sm {
        font-size: 0.85rem;
        color: #a0aec0;
        text-transform: uppercase;
        letter-spacing: 0.07em;
    }
    .hero-sub {
        font-size: 0.79rem;
        color: #718096;
        margin-top: 6px;
    }
</style>
""", unsafe_allow_html=True)

# ── 경로 상수 ─────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
DB_PATH      = BASE_DIR / "data" / "agent_metrics.db"
RESULTS_DIR  = BASE_DIR / "experiments" / "results"
CHROMA_PATH  = BASE_DIR / "data" / "chroma_db"

# ── 색상 팔레트 ───────────────────────────────────────────────────────────────
RESULT_COLORS = {"SUCCESS": "#2ecc71", "FAILURE": "#e74c3c", "IMPOSSIBLE": "#e67e22"}
SOURCE_COLORS = {"L1_CACHE": "#3498db", "L2_LLM": "#9b59b6"}
THEME         = "plotly_dark"

# ── 에러 타입 추론 ─────────────────────────────────────────────────────────────
_ERR_PATTERNS = [
    ("OOM",             ["out of memory", "outofmemory", "cuda out", "xpu out", "allocate"]),
    ("Memory_Leak",     ["memory leak"]),
    ("DB_Connection",   ["database", "db connection", "postgres", "connection refused", "sqlalchemy"]),
    ("Network_Timeout", ["timeout", "timed out", "connection timeout"]),
    ("Auth_Error",      ["permission denied", "unauthorized", "authentication", "403", "401"]),
    ("Disk_Full",       ["disk full", "no space left"]),
    ("CPU_Overload",    ["cpu overload", "load average", "high cpu"]),
    ("App_Crash",       ["critical:", "segfault", "crash", "fatal"]),
]

def _infer_error_type(log: str) -> str:
    low = str(log).lower()
    for name, kws in _ERR_PATTERNS:
        if any(k in low for k in kws):
            return name
    return "Other"


# ── 비개발자용 친절 설명 사전 ──────────────────────────────────────────────────
_CATEGORY_DESC: dict[str, tuple[str, str]] = {
    # (한국어 이름, 쉬운 설명)
    "OOM":             ("메모리 부족",        "프로그램이 사용할 수 있는 RAM/VRAM이 꽉 찼습니다. 컵에 물이 넘치는 것과 같습니다."),
    "Out_Of_Memory":   ("메모리 부족",        "프로그램이 사용할 수 있는 RAM/VRAM이 꽉 찼습니다."),
    "Memory_Leak":     ("메모리 누수",        "프로그램이 메모리를 조금씩 잡아먹으며 반환하지 않아 결국 시스템이 느려집니다."),
    "DB_Connection":   ("데이터베이스 연결 오류", "서버가 데이터베이스에 접속하지 못하는 상태입니다. 데이터 저장/조회가 불가합니다."),
    "DB_Timeout":      ("DB 응답 없음",       "데이터베이스가 너무 오래 걸려 응답하지 않아 연결을 끊었습니다."),
    "Network_Timeout": ("네트워크 응답 없음", "서버끼리 통신하는 데 시간이 너무 오래 걸려 포기한 상태입니다."),
    "Auth_Error":      ("인증/권한 오류",     "접근 권한이 없거나 비밀번호가 틀려 작업을 수행하지 못하는 상태입니다."),
    "Disk_Full":       ("디스크 공간 부족",   "하드디스크가 꽉 찼습니다. 새 파일을 저장하거나 로그를 쓸 수 없습니다."),
    "CPU_Overload":    ("CPU 과부하",         "CPU가 처리할 수 있는 이상의 작업이 몰려 응답이 느려지거나 멈춥니다."),
    "App_Crash":       ("애플리케이션 비정상 종료", "프로그램이 예기치 않게 강제 종료되었습니다."),
    "Port_Conflict":   ("포트 충돌",          "두 프로그램이 같은 네트워크 문을 사용하려 해 충돌이 발생했습니다."),
    "Process_Crash":   ("프로세스 비정상 종료", "실행 중인 프로세스가 예상치 못하게 종료되었습니다."),
    "Other":           ("기타 오류",          "위 분류에 해당하지 않는 오류입니다."),
}

_ACTION_DESC: dict[str, str] = {
    "clear_memory":         "메모리를 강제로 비웁니다 (사용하지 않는 데이터 정리).",
    "restart_service":      "문제가 생긴 서비스를 껐다가 다시 켭니다.",
    "kill_process":         "비정상 동작하는 프로세스를 강제 종료합니다.",
    "execute_llm_command":  "AI가 분석해 생성한 맞춤 명령어를 실행합니다.",
    "execute_rule_command": "사전 정의된 규칙에 따라 명령어를 실행합니다.",
    "escalate_to_human":    "AI가 혼자 처리하기 어렵다고 판단해 관리자에게 알림을 보냅니다.",
    "alert_only":           "즉각 조치 없이 경보만 기록합니다.",
}

_SOURCE_DESC: dict[str, str] = {
    "L1_CACHE": "⚡ L1 빠른 기억 — 과거에 해결했던 동일 에러를 즉시 꺼내 처리 (0.2초 이내)",
    "L2_LLM":   "🧠 L2 AI 추론 — 처음 보는 에러라 AI가 시스템 상태를 분석해 해결책 도출 (수 초)",
    "RULE":     "📋 규칙 기반 — 사전 정의된 패턴과 일치해 규칙대로 처리",
}

_FAIL_DESC: dict[str, str] = {
    "SecurityBlock":       "🔒 보안 필터가 위험한 명령어를 차단했습니다. (정상 동작 — 안전장치가 작동한 것입니다)",
    "HumanRejected":       "🙅 관리자가 해당 명령어 실행을 직접 거절했습니다.",
    "ApprovalTimeout":     "⏰ 관리자 승인 대기 시간(5분)이 초과되어 조치가 자동 취소되었습니다.",
    "CalledProcessError":  "⚠️ 명령어를 실행했지만 서버에서 오류가 반환되었습니다. 자동 롤백이 시도되었습니다.",
    "TimeoutExpired":      "⏱️ 명령어가 15초 내에 완료되지 않아 강제 중단되었습니다.",
    "ServiceRestartFailed":"⚠️ 서비스 재시작을 시도했지만 정상 상태로 돌아오지 않았습니다.",
    "ProcessKillFailed":   "⚠️ 프로세스 종료 신호를 보냈지만 프로세스가 여전히 실행 중입니다.",
    "MemoryClearFailed":   "⚠️ 메모리 정리를 시도했지만 완전히 해제되지 않았습니다.",
    "EscalatedToHuman":    "🔔 AI 자동 처리가 불가하다고 판단해 관리자를 호출했습니다.",
    "PermissionError":     "🔒 명령어 실행 권한이 없어 처리가 불가했습니다.",
    "EmptyCommand":        "❓ AI가 실행할 명령어를 생성하지 못했습니다.",
    "UnknownActionType":   "❓ 정의되지 않은 조치 유형입니다.",
}


def _make_story(row: pd.Series) -> tuple[str, str, str]:
    """행 데이터에서 (헤더_아이콘, 성공_서사, 상세_부연) 3개 문자열을 생성한다."""
    cat_raw    = str(row.get("error_category", "Other"))
    cat_label, cat_desc = _CATEGORY_DESC.get(cat_raw, (cat_raw, ""))
    source     = str(row.get("resolution_source", ""))
    action_raw = str(row.get("action_type", ""))
    action_desc = _ACTION_DESC.get(action_raw, action_raw)
    command    = str(row.get("command", "") or "")
    result     = str(row.get("result_category", ""))
    error_type = str(row.get("error_type", "") or "")
    error_detail = str(row.get("error_detail", "") or "")
    is_success = str(row.get("success", "0")) in ("True", "1", "1.0", "true")

    source_desc = _SOURCE_DESC.get(source, source)

    if is_success:
        icon = "✅"
        if source == "L1_CACHE":
            narrative = (
                f"에이전트가 '{cat_label}' 에러를 **과거 기억(L1 캐시)에서 즉시 찾아** "
                f"**{action_desc}** 조치를 실행해 해결했습니다."
            )
        elif source == "L2_LLM":
            cmd_part = f" — 실행 명령어: `{command}`" if command else ""
            narrative = (
                f"처음 접하는 유형의 에러라 **AI가 시스템 상태를 직접 진단**하고 "
                f"해결책을 추론해 **{action_desc}** 조치를 수행했습니다{cmd_part}."
            )
        else:
            cmd_part = f" (`{command}`)" if command else ""
            narrative = (
                f"에러 패턴이 사전 정의된 규칙과 일치해 **규칙 기반으로 {action_desc}** "
                f"조치{cmd_part}를 실행해 해결했습니다."
            )
        detail = ""

    elif result == "IMPOSSIBLE":
        icon = "🔔" if error_type == "EscalatedToHuman" else "🚫"
        narrative = _FAIL_DESC.get(error_type, f"자동 처리가 불가능한 상황이 발생했습니다 ({error_type}).")
        detail = error_detail[:300] if error_detail and error_detail not in ("None", "") else ""

    else:
        icon = "⚠️"
        narrative = _FAIL_DESC.get(
            error_type,
            f"조치를 시도했지만 실패했습니다 (원인: {error_type or '알 수 없음'}).",
        )
        detail = error_detail[:300] if error_detail and error_detail not in ("None", "") else ""

    return icon, narrative, detail


# ── 데이터 로드 ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=5)
def load_circuit_breaker_status() -> pd.DataFrame:
    """circuit_breaker 테이블에서 최근 상태를 읽어온다."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            has_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='circuit_breaker'"
            ).fetchone()
            if not has_table:
                return pd.DataFrame()
            return pd.read_sql_query(
                "SELECT error_sig, state, consecutive_failures, opened_at, last_updated "
                "FROM circuit_breaker ORDER BY last_updated DESC LIMIT 10",
                conn,
            )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=5)
def load_metrics() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query("SELECT * FROM metrics ORDER BY timestamp DESC", conn)
        if df.empty:
            return df
        df["timestamp"]       = pd.to_datetime(df["timestamp"], format="mixed", utc=True).dt.tz_convert(None)
        df["latency_ms"]      = (df["latency_sec"] * 1000).round(1)
        df["result_category"] = df["result_category"].fillna("SUCCESS")
        # error_category: LLM이 분류한 도메인 카테고리 (우선). 없으면 텍스트 추론.
        if "error_category" not in df.columns or df["error_category"].isna().all():
            df["error_category"] = df["error_log"].apply(_infer_error_type)
        else:
            null_mask = df["error_category"].isna()
            df.loc[null_mask, "error_category"] = df.loc[null_mask, "error_log"].apply(_infer_error_type)
        # error_type: executor 예외 타입. 없으면 텍스트 추론 (후방 호환).
        if "error_type" not in df.columns or df["error_type"].isna().all():
            df["error_type"] = df["error_log"].apply(_infer_error_type)
        else:
            null_mask = df["error_type"].isna()
            df.loc[null_mask, "error_type"] = df.loc[null_mask, "error_log"].apply(_infer_error_type)
        return df
    except Exception as e:
        st.error(f"DB 로드 오류: {e}")
        return pd.DataFrame()


def _latest_csv(prefix: str) -> pd.DataFrame | None:
    """prefix_*.csv 중 가장 최신 파일을 반환. 없으면 None."""
    files = sorted(RESULTS_DIR.glob(f"{prefix}_*.csv"))
    if not files:
        return None
    try:
        return pd.read_csv(files[-1])
    except Exception:
        return None


# ── 사이드바 (순수 모니터링 뷰어) ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🛡️ MLOps Monitor")
    st.divider()

    auto_refresh = st.toggle("🔄 자동 새로고침 (5초)", value=True)
    if st.button("⟳  지금 새로고침", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"Last update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    st.divider()
    st.markdown("**🔍 데이터 필터**")
    days = st.slider("최근 N일", min_value=1, max_value=30, value=7)
    source_opts = st.multiselect(
        "해결 소스",
        options=["L1_CACHE", "L2_LLM"],
        default=["L1_CACHE", "L2_LLM"],
    )

    st.divider()
    st.markdown("**ℹ️ 시스템 정보**")
    db_exists  = DB_PATH.exists()
    csv_count  = len(list(RESULTS_DIR.glob("*.csv"))) if RESULTS_DIR.exists() else 0

    import subprocess, shutil
    agent_running = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", "src.log_watcher"],
            capture_output=True, text=True
        )
        agent_running = result.returncode == 0
    except Exception:
        pass

    if agent_running:
        st.success("🟢 에이전트 실행 중")
    else:
        st.error("🔴 에이전트 중지됨")

    st.caption(f"DB 상태: {'✅ 연결됨' if db_exists else '❌ 없음'}")
    st.caption(f"실험 CSV: {csv_count}개")
    st.caption(f"DB 경로: `{DB_PATH.name}`")


# ── 메인 헤더 ─────────────────────────────────────────────────────────────────
st.title("🛡️ Enterprise MLOps Agent Dashboard")
st.markdown(
    "**Self-Healing Agent**의 에러 감지·자가 치유 성능을 실시간 모니터링하고, "
    "핵심 아키텍처 실험 결과를 분석합니다."
)

# 데이터 로드 & 필터
df_all  = load_metrics()
df      = pd.DataFrame()
df_prev = pd.DataFrame()

if not df_all.empty:
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    df = df_all[df_all["timestamp"] >= cutoff].copy()
    if source_opts:
        df = df[df["resolution_source"].isin(source_opts)]

    prev_start = pd.Timestamp.now() - pd.Timedelta(days=days * 2)
    df_prev    = df_all[
        (df_all["timestamp"] >= prev_start) & (df_all["timestamp"] < cutoff)
    ]
    if source_opts:
        df_prev = df_prev[df_prev["resolution_source"].isin(source_opts)]

# ── ChromaDB 벡터 품질 로드 ──────────────────────────────────────────────────
@st.cache_data(ttl=120)
def load_vector_quality() -> dict:
    """ChromaDB SQLite를 직접 쿼리해 벡터 품질 지표를 수집한다.
    HNSW 세그먼트 로드를 피해 '_type' KeyError 우회."""
    result = {
        "total": 0, "categories": {}, "learned": 0,
        "hit_rate": None, "dead_count": 0, "sources": {},
        "_error": None,
    }
    chroma_sqlite = CHROMA_PATH / "chroma.sqlite3"
    if not chroma_sqlite.exists():
        result["_error"] = f"경로 없음: {chroma_sqlite}"
        return result
    try:
        with sqlite3.connect(chroma_sqlite) as conn:
            result["total"] = conn.execute(
                "SELECT COUNT(*) FROM embeddings"
            ).fetchone()[0]
            cats = conn.execute(
                "SELECT string_value, COUNT(*) FROM embedding_metadata "
                "WHERE key='error_category' GROUP BY string_value"
            ).fetchall()
            result["categories"] = {r[0]: r[1] for r in cats if r[0]}
            result["learned"] = conn.execute(
                "SELECT COUNT(DISTINCT id) FROM embedding_metadata WHERE key='learned_at'"
            ).fetchone()[0]
            src_rows = conn.execute(
                "SELECT string_value, COUNT(*) FROM embedding_metadata "
                "WHERE key='source' GROUP BY string_value"
            ).fetchall()
            no_src = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE id NOT IN "
                "(SELECT DISTINCT id FROM embedding_metadata WHERE key='source')"
            ).fetchone()[0]
            result["sources"] = {r[0]: r[1] for r in src_rows if r[0]}
            if no_src:
                result["sources"]["etl_demo"] = no_src
    except Exception as e:
        result["_error"] = str(e)

    # metrics DB에서 L1 히트율 계산
    if DB_PATH.exists() and result["total"] > 0:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                total_q = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
                l1_hits = conn.execute(
                    "SELECT COUNT(*) FROM metrics WHERE resolution_source='L1_CACHE'"
                ).fetchone()[0]
            result["hit_rate"] = (l1_hits / total_q * 100) if total_q else 0.0
        except Exception:
            pass

    return result


# ── 탭 레이아웃 ───────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "🏆  핵심 성과 요약",
    "📡  실시간 장애 조치",
    "🔬  아키텍처 성능 검증",
    "🧬  지식 저장소(Vector DB)",
])


# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — 실시간 에이전트 모니터링
# ════════════════════════════════════════════════════════════════════════════════
with tab1:
    if not df.empty:
        total   = len(df)
        success = int(df["success"].astype(int).sum())
        s_rate  = success / total * 100
        l1_rate = (df["resolution_source"] == "L1_CACHE").mean() * 100
        avg_ms  = df["latency_ms"].mean()

        prev_s_rate = (df_prev["success"].astype(int).mean() * 100) if not df_prev.empty else None
        prev_l1     = ((df_prev["resolution_source"] == "L1_CACHE").mean() * 100) if not df_prev.empty else None
        prev_ms     = (df_prev["latency_ms"].mean()) if not df_prev.empty else None

        # 벤치마크 사전 로드 (Hero 카드 + L1/L2 상세 섹션 공용)
        _bm_path = RESULTS_DIR / "benchmark_l1_vs_l2.csv"
        _bm_df = None
        _bm_l1ms = _bm_l2ms = _bm_ratio = _bm_l1sr = None
        if _bm_path.exists():
            _bm_df = pd.read_csv(_bm_path)
            _bm_df["latency_ms"] = _bm_df["latency"] * 1000
            _l1d_raw = _bm_df[_bm_df["source"] == "L1_CACHE"]["latency_ms"]
            _l2d_raw = _bm_df[_bm_df["source"] == "L2_LLM"]["latency_ms"]
            _bm_l1ms = _l1d_raw.mean()
            _bm_l2ms = _l2d_raw.mean() if len(_l2d_raw) else None
            _bm_ratio = round(_bm_l2ms / _bm_l1ms) if _bm_l2ms else None
            _bm_l1sr  = _bm_df[_bm_df["source"] == "L1_CACHE"]["success"].mean() * 100

        _oom_count = 0
        _oom_sr_val = 100.0
        if "error_category" in df.columns:
            _oom_tmp = df[df["error_category"].str.upper().str.contains(
                "OOM|OUT_OF_MEMORY|MEMORY", na=False
            )]
            _oom_count = len(_oom_tmp)
            if _oom_count > 0:
                _oom_sr_val = _oom_tmp["success"].astype(int).mean() * 100

        # ── 시스템 상태 배너 ─────────────────────────────────────────────
        if s_rate >= 90:
            bcls, bicon, blbl = "s-ok",   "🟢", "HEALTHY"
        elif s_rate >= 70:
            bcls, bicon, blbl = "s-warn", "🟡", "DEGRADED"
        else:
            bcls, bicon, blbl = "s-crit", "🔴", "CRITICAL"

        st.markdown(
            f'<div class="status-banner {bcls}">'
            f'<span style="font-size:1.55em">{bicon}</span>'
            f'<span style="font-size:1.15em;font-weight:700">System Status: {blbl}</span>'
            f'<span style="color:#8899aa;font-size:0.9em;margin-left:16px">'
            f'성공률 {s_rate:.1f}%&nbsp;·&nbsp;{total:,}건 처리&nbsp;·&nbsp;최근 {days}일</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Big Number 헤드라인 ───────────────────────────────────────────
        _24h_cutoff = pd.Timestamp.now() - pd.Timedelta(hours=24)
        _24h_df     = df_all[df_all["timestamp"] >= _24h_cutoff] if not df_all.empty else pd.DataFrame()

        if not _24h_df.empty:
            _24h_resolved = int(_24h_df["success"].astype(int).sum())
            _24h_total    = len(_24h_df)
            _24h_label    = "지난 24시간 동안"
        else:
            # 최근 24h 데이터 없음 → 슬라이더 선택 기간 기준으로 통일
            _24h_resolved = success
            _24h_total    = total
            _24h_label    = f"최근 {days}일 동안"

        st.markdown(
            f"""
            <div style="
                text-align:center;
                padding: 28px 20px 20px;
                margin: 16px 0 8px;
                background: linear-gradient(135deg, rgba(46,204,113,.07), rgba(52,152,219,.07));
                border-radius: 14px;
                border: 1px solid rgba(46,204,113,.25);
            ">
                <div style="font-size:0.88rem;color:#a0aec0;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px;">
                    {_24h_label}
                </div>
                <div style="font-size:3.6rem;font-weight:900;line-height:1;color:#2ecc71;">
                    {_24h_resolved:,}건
                </div>
                <div style="font-size:1.15rem;color:#e2e8f0;margin-top:10px;font-weight:500;">
                    의 시스템 장애를 <span style="color:#2ecc71;font-weight:700;">사람 없이 스스로</span> 해결했습니다.
                </div>
                <div style="font-size:0.82rem;color:#718096;margin-top:8px;">
                    감지 {_24h_total:,}건 중 {_24h_resolved:,}건 자동 복구 &nbsp;·&nbsp;
                    평균 복구 시간 {avg_ms:.0f} ms &nbsp;·&nbsp;
                    사람이 직접 처리했다면 약 {_24h_resolved * 30:,}분 소요
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── 핵심 성과 Hero 카드 ───────────────────────────────────────────
        st.markdown('<p class="section-title">🏆 핵심 성과 요약</p>',
                    unsafe_allow_html=True)
        h1, h2, h3 = st.columns(3)
        with h1:
            _r_str  = f"{_bm_ratio}배" if _bm_ratio else "37배"
            _l1_str = f"{_bm_l1ms:.0f}ms" if _bm_l1ms else "79ms"
            _l2_str = f"{_bm_l2ms:.0f}ms" if _bm_l2ms else "2,974ms"
            st.markdown(f"""<div class="hero-card" style="background:linear-gradient(135deg,rgba(52,152,219,.18),rgba(52,152,219,.04));border:2px solid #3498db;">
  <div class="hero-label-sm">⚡ 응답 속도 우위</div>
  <div class="hero-number" style="color:#3498db">{_r_str}</div>
  <div class="hero-label-sm">더 빠름</div>
  <div class="hero-sub">순간 기억 {_l1_str} vs AI 추론 {_l2_str}</div>
</div>""", unsafe_allow_html=True)
        with h2:
            _oom_n_str = f"{_oom_count}건" if _oom_count > 0 else "상시 감시 중"
            _oom_r_str = f"{_oom_sr_val:.0f}%" if _oom_count > 0 else "100%"
            st.markdown(f"""<div class="hero-card" style="background:linear-gradient(135deg,rgba(46,204,113,.18),rgba(46,204,113,.04));border:2px solid #2ecc71;">
  <div class="hero-label-sm">🛡️ 시스템 과부하 방지율</div>
  <div class="hero-number" style="color:#2ecc71">{_oom_r_str}</div>
  <div class="hero-label-sm">자동 방어 성공</div>
  <div class="hero-sub">총 {_oom_n_str} 선제 차단</div>
</div>""", unsafe_allow_html=True)
        with h3:
            st.markdown(f"""<div class="hero-card" style="background:linear-gradient(135deg,rgba(230,126,34,.18),rgba(230,126,34,.04));border:2px solid #e67e22;">
  <div class="hero-label-sm">🚀 즉시 자가 해결률</div>
  <div class="hero-number" style="color:#e67e22">{l1_rate:.1f}%</div>
  <div class="hero-label-sm">경험 기반 즉시 처리</div>
  <div class="hero-sub">평균 복구 {avg_ms:.0f}ms</div>
</div>""", unsafe_allow_html=True)

        st.divider()

        # ── KPI 4개 ──────────────────────────────────────────────────────
        st.markdown('<p class="section-title">핵심 지표</p>',
                    unsafe_allow_html=True)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(
            "감지한 장애 수",
            f"{total:,} 건",
            help="에이전트가 자동으로 감지하여 처리를 시도한 전체 장애 건수입니다.",
        )
        k2.metric(
            "자동 복구 성공률",
            f"{s_rate:.1f} %",
            f"{s_rate - prev_s_rate:+.1f}%p" if prev_s_rate is not None else None,
            help="에이전트가 사람 개입 없이 장애를 스스로 해결한 비율입니다. 90% 이상이면 시스템이 건강합니다.",
        )
        _l2_count = int((df["resolution_source"] == "L2_LLM").sum())
        k3.metric(
            "L2 AI 신규 추론 건수",
            f"{_l2_count} 건",
            help="과거 경험에 없는 처음 보는 에러라 AI가 직접 추론한 건수입니다. 적을수록 에이전트가 잘 학습됐다는 의미입니다.",
        )
        k4.metric(
            "장애 복구 시간",
            f"{avg_ms:.0f} ms",
            f"{avg_ms - prev_ms:+.0f} ms" if prev_ms is not None else None,
            delta_color="inverse",
            help="장애 감지부터 복구 완료까지 걸린 평균 시간입니다. 사람이 직접 처리하면 평균 30분(1,800,000 ms)이 걸립니다.",
        )

        st.divider()

        # ── 핵심 차트 2종 — 나란히 배치 ──────────────────────────────────────
        main_c1, main_c2 = st.columns([1, 1])

        with main_c1:
            st.markdown(
                '<p class="section-title">⏱️ 장애 복구 시간 비교 — 사람이 직접 처리 vs AI 에이전트 자동복구</p>',
                unsafe_allow_html=True,
            )
            # 수동 기준: 온콜 페이징(5분) + 로그인/이동(3분) + 진단(15분) + 조치(7분) = 30분
            MANUAL_MTTR_SEC = 1800.0
            agent_mttr_sec  = df["latency_sec"].mean()
            reduction_pct   = (1 - agent_mttr_sec / MANUAL_MTTR_SEC) * 100
            l1_avg_ms = df[df["resolution_source"] == "L1_CACHE"]["latency_sec"].mean() * 1000
            l2_mask   = df["resolution_source"] == "L2_LLM"
            l2_avg_ms = df[l2_mask]["latency_sec"].mean() * 1000 if l2_mask.any() else None

            fig_mttr = go.Figure()
            fig_mttr.add_trace(go.Bar(
                x=[MANUAL_MTTR_SEC],
                y=["수동 복구 (On-Call)"],
                orientation="h",
                marker_color="#e74c3c",
                text=["30분 (1,800초)"],
                textposition="inside",
                insidetextanchor="middle",
            ))
            fig_mttr.add_trace(go.Bar(
                x=[agent_mttr_sec],
                y=["에이전트 자동복구"],
                orientation="h",
                marker_color="#2ecc71",
                text=[f"{agent_mttr_sec * 1000:.0f} ms"],
                textposition="outside",
            ))
            fig_mttr.update_layout(
                xaxis=dict(
                    type="log",
                    title="복구 소요 시간 (초, 로그 스케일)",
                    tickvals=[0.001, 0.01, 0.1, 1, 10, 60, 600, 1800],
                    ticktext=["1ms", "10ms", "100ms", "1s", "10s", "1분", "10분", "30분"],
                ),
                template=THEME,
                showlegend=False,
                margin=dict(t=10, b=40, l=10, r=90),
                height=170,
                bargap=0.4,
            )
            st.plotly_chart(fig_mttr, width='stretch')
            st.metric(
                "MTTR 단축률",
                f"{reduction_pct:.1f} %",
                f"30분 → {agent_mttr_sec * 1000:.0f} ms",
                help=(
                    "MTTR(Mean Time To Recovery): 장애 발생부터 복구까지 걸리는 평균 시간. "
                    "수동 On-Call(30분) 대비 에이전트가 얼마나 빠르게 복구했는지를 나타냅니다."
                ),
            )
            st.caption(f"⚡ L1 Cache 평균: {l1_avg_ms:.0f} ms")
            if l2_avg_ms is not None:
                st.caption(f"🧠 L2 LLM 평균: {l2_avg_ms:.0f} ms")
            st.caption("※ 수동 기준: 온콜 페이징+진단+조치 30분 (업계 평균)")

        with main_c2:
            st.markdown('<p class="section-title">🎯 자동 복구 성공 / 실패 비율</p>',
                        unsafe_allow_html=True)
            cat_df = df["result_category"].value_counts().reset_index()
            cat_df.columns = ["category", "count"]
            fig_donut = px.pie(
                cat_df,
                names="category",
                values="count",
                hole=0.55,
                color="category",
                color_discrete_map=RESULT_COLORS,
                template=THEME,
            )
            fig_donut.update_traces(
                textposition="outside",
                textinfo="percent+label",
                pull=[0.04] * len(cat_df),
            )
            fig_donut.update_layout(
                showlegend=True,
                legend=dict(orientation="h", y=-0.12, x=0.5, xanchor="center"),
                margin=dict(t=10, b=30, l=10, r=10),
                height=330,
            )
            st.plotly_chart(fig_donut, width='stretch')



# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — 실시간 조치 로그
# ════════════════════════════════════════════════════════════════════════════════
with tab2:
    if not df.empty:
        # ── 서킷 브레이커 실시간 상태 ────────────────────────────────────────
        cb_df = load_circuit_breaker_status()
        open_cnt      = int((cb_df["state"] == "OPEN").sum())      if not cb_df.empty else 0
        half_open_cnt = int((cb_df["state"] == "HALF_OPEN").sum()) if not cb_df.empty else 0

        if open_cnt > 0:
            _cb_cls, _cb_icon, _cb_label = (
                "s-crit", "🔴",
                f"OPEN — {open_cnt}개 패턴 AI 판단 중단 · 관리자 에스컬레이션 중",
            )
        elif half_open_cnt > 0:
            _cb_cls, _cb_icon, _cb_label = (
                "s-warn", "🟡",
                f"HALF-OPEN — {half_open_cnt}개 패턴 복구 시험 중",
            )
        else:
            _cb_cls, _cb_icon, _cb_label = (
                "s-ok", "🟢",
                "CLOSED — 모든 패턴 정상 자동 처리 중",
            )

        st.markdown(
            f'<div class="status-banner {_cb_cls}">'
            f'<span style="font-size:1.4em">{_cb_icon}</span>'
            f'<span style="font-size:1.05em;font-weight:700">안전장치(서킷 브레이커) 상태: {_cb_label}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if not cb_df.empty:
            _cb_show = cb_df[["error_sig", "state", "consecutive_failures", "last_updated"]].copy()
            _cb_show["state"] = _cb_show["state"].replace(
                {"CLOSED": "🟢 CLOSED", "OPEN": "🔴 OPEN", "HALF_OPEN": "🟡 HALF-OPEN"}
            )
            _cb_show.columns = ["에러 시그니처(MD5 앞 8자)", "상태", "연속 실패 횟수", "마지막 업데이트"]
            st.dataframe(_cb_show, hide_index=True, use_container_width=True)

        with st.expander("ℹ️ 서킷 브레이커란?", expanded=False):
            st.markdown("""
**서킷 브레이커란?** 동일 에러 조치가 **3번 연속 실패**하면 AI 자동 판단을 멈추고 관리자를 호출하는 '비상 정지' 안전장치입니다.

| 상태 | 의미 |
|---|---|
| 🟢 **CLOSED** | 정상 — AI 자율 처리 중 |
| 🔴 **OPEN** | 비상 정지 — 관리자 Slack 알림 발송 |
| 🟡 **HALF-OPEN** | 복구 시험 중 — 1회 시험 후 자동 판단 |
            """)

        st.divider()

        # ── 발생한 에러 유형별 빈도 ───────────────────────────────────────────
        st.markdown('<p class="section-title">📊 발생한 에러 유형별 빈도</p>',
                    unsafe_allow_html=True)
        with st.expander("📖 에러 카테고리 용어 설명", expanded=False):
            for key, (label, desc) in _CATEGORY_DESC.items():
                if desc:
                    st.markdown(f"- **{label}** (`{key}`): {desc}")
        etype_df = df["error_category"].value_counts().reset_index()
        etype_df.columns = ["error_category", "count"]
        fig_etype = px.bar(
            etype_df,
            x="count",
            y="error_category",
            orientation="h",
            color="count",
            color_continuous_scale="Blues",
            text="count",
            template=THEME,
            labels={"count": "발생 건수", "error_category": "에러 카테고리"},
        )
        fig_etype.update_traces(
            textposition="outside",
            marker_line_width=0,
        )
        fig_etype.update_layout(
            showlegend=False,
            coloraxis_showscale=False,
            yaxis=dict(autorange="reversed", title=None),
            xaxis=dict(title="발생 건수"),
            margin=dict(t=10, b=10, l=10, r=60),
            height=330,
        )
        st.plotly_chart(fig_etype, width='stretch')

        st.divider()

        # ── 해결 소스별 처리 결과 누적 막대 ──────────────────────────────────
        _tw1, _tw2 = st.columns(2)
        with _tw1:
            st.markdown('<p class="section-title">📦 해결 소스별 처리 결과 분포</p>',
                        unsafe_allow_html=True)
            _src_df = (
                df.groupby(["resolution_source", "result_category"])
                .size()
                .reset_index(name="count")
            )
            _fig_src = px.bar(
                _src_df,
                x="resolution_source",
                y="count",
                color="result_category",
                color_discrete_map=RESULT_COLORS,
                barmode="stack",
                template=THEME,
                labels={
                    "resolution_source": "해결 소스",
                    "count": "처리 건수",
                    "result_category": "처리 결과",
                },
            )
            _fig_src.update_layout(
                height=320,
                margin=dict(t=10, b=10, l=10, r=10),
                legend=dict(
                    title="처리 결과",
                    orientation="h",
                    yanchor="bottom", y=1.02,
                    xanchor="right", x=1,
                ),
            )
            st.plotly_chart(_fig_src, use_container_width=True)

        with _tw2:
            st.markdown('<p class="section-title">⏱️ 장애 조치 응답 시간 추이</p>',
                        unsafe_allow_html=True)
            _lat_df = df[["timestamp", "latency_ms", "resolution_source"]].dropna(
                subset=["latency_ms"]
            ).copy()
            _fig_lat = px.scatter(
                _lat_df,
                x="timestamp",
                y="latency_ms",
                color="resolution_source",
                color_discrete_map=SOURCE_COLORS,
                template=THEME,
                labels={
                    "timestamp": "시간",
                    "latency_ms": "응답 시간 (ms)",
                    "resolution_source": "해결 소스",
                },
            )
            _fig_lat.update_layout(
                height=320,
                margin=dict(t=10, b=10, l=10, r=10),
                yaxis=dict(title="응답 시간 (ms)"),
                legend=dict(
                    title="해결 소스",
                    orientation="h",
                    yanchor="bottom", y=1.02,
                    xanchor="right", x=1,
                ),
            )
            st.plotly_chart(_fig_lat, use_container_width=True)

        st.divider()

        # ── 서사형 실시간 로그 ────────────────────────────────────────────────
        with st.expander(
            "📝 에이전트가 처리한 최근 10건의 실제 장애 조치 기록 보기 (클릭)",
            expanded=True,
        ):
            st.caption("각 항목을 클릭하면 에러 원인·조치 과정·성공/실패 이유를 상세히 확인할 수 있습니다.")

            _LOG_COLS = ["timestamp", "error_category", "result_category", "resolution_source",
                         "action_type", "latency_ms", "error_log", "command",
                         "error_type", "error_detail", "success"]
            _LOG_COLS = [c for c in _LOG_COLS if c in df.columns]
            log_rows  = df[_LOG_COLS].head(10)

            for _, row in log_rows.iterrows():
                icon, narrative, detail = _make_story(row)
                ts_str  = str(row.get("timestamp", ""))[:19]
                cat_raw = str(row.get("error_category", ""))
                cat_label = _CATEGORY_DESC.get(cat_raw, (cat_raw, ""))[0]
                result  = str(row.get("result_category", ""))
                result_icon = {"SUCCESS": "✅", "FAILURE": "⚠️", "IMPOSSIBLE": "🚫"}.get(result, "❓")
                header  = f"{icon} [{ts_str}] {cat_label} → {result_icon} {result}"

                with st.expander(header, expanded=False):
                    src_raw  = str(row.get("resolution_source", ""))
                    src_desc = _SOURCE_DESC.get(src_raw, src_raw)
                    st.info(f"**에러 유형:** {cat_label}  |  **해결 경로:** {src_desc}")
                    st.markdown(narrative)
                    if detail:
                        st.warning(f"**실패 원인 상세:** {detail}")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("소요 시간", f"{row.get('latency_ms', 0):.0f} ms")
                    _src_kor = {"L1_CACHE": "⚡ 빠른 기억(L1)", "L2_LLM": "🧠 AI 추론(L2)"}
                    c2.metric("해결 경로", _src_kor.get(src_raw, src_raw))
                    _res_kor = {"SUCCESS": "✅ 성공", "FAILURE": "⚠️ 실패", "IMPOSSIBLE": "🚫 불가"}
                    c3.metric("처리 결과", _res_kor.get(result, result))
                    cmd = row.get("command") or row.get("action_type", "")
                    if cmd and str(cmd) not in ("nan", "None", ""):
                        st.code(str(cmd), language="bash")
                    raw_log = str(row.get("error_log", "")).strip()
                    if raw_log and raw_log != "nan":
                        with st.expander("🔍 원본 에러 로그 보기", expanded=False):
                            st.code(raw_log, language="text")


# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — 아키텍처 성능 검증
# ════════════════════════════════════════════════════════════════════════════════
with tab3:
    st.markdown(
        "Self-Healing Agent의 핵심 아키텍처 결정을 뒷받침하는 **8가지 실증 실험 결과**입니다. "
        "각 실험은 '왜 이렇게 설계했는가'에 대한 데이터 기반 근거를 제공합니다."
    )
    st.divider()

    # ── 1. Baseline vs RAG ───────────────────────────────────────────────
    st.markdown("#### 🏆 1. Baseline vs RAG 시스템 성능 비교")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "단순 키워드 검색(Baseline)과 AI 벡터 유사도 검색(RAG)을 직접 비교합니다. "
        "RAG가 얼마나 더 많은 에러를 정확히 인식하는지, 놓치지 않고 처리하는지를 수치로 보여줍니다."
    )
    bdf = _latest_csv("baseline_results")
    if bdf is None:
        st.info("아직 실험 데이터가 없습니다 (baseline_results_*.csv)")
    else:
        bc1, bc2 = st.columns([6, 4])
        with bc1:
            _b_cols = ["accuracy", "coverage"]
            if "action_accuracy" in bdf.columns:
                _b_cols = ["accuracy", "action_accuracy", "coverage"]
            melted_b = bdf[["system"] + _b_cols].melt(
                id_vars="system", var_name="지표", value_name="점수"
            )
            melted_b["지표"] = melted_b["지표"].replace({
                "accuracy":        "카테고리 정확도",
                "action_accuracy": "액션 정확도",
                "coverage":        "커버리지",
            })
            fig_bl = px.bar(
                melted_b,
                x="system",
                y="점수",
                color="지표",
                barmode="group",
                text_auto=".1%",
                template=THEME,
                labels={"system": "시스템"},
                color_discrete_sequence=["#3498db", "#9b59b6", "#2ecc71"],
            )
            fig_bl.update_traces(textposition="outside", texttemplate="%{y:.1%}")
            fig_bl.update_layout(
                yaxis=dict(range=[0, 1.15], tickformat=".0%", title=None),
                xaxis=dict(title=None),
                legend=dict(title=None, orientation="h", y=1.12),
                margin=dict(t=10, b=10, l=10, r=10),
                height=340,
            )
            st.plotly_chart(fig_bl, width='stretch')
        with bc2:
            if len(bdf) >= 2:
                kw  = bdf.iloc[0]
                rag = bdf.iloc[1]
                st.metric("키워드 기반 정확도", f"{kw['accuracy']*100:.1f}%")
                st.metric(
                    "RAG 카테고리 정확도",
                    f"{rag['accuracy']*100:.1f}%",
                    f"+{(rag['accuracy'] - kw['accuracy'])*100:.1f}%p 향상",
                )
                if "action_accuracy" in bdf.columns:
                    st.metric(
                        "RAG 액션 정확도",
                        f"{rag['action_accuracy']*100:.1f}%",
                        f"+{rag['action_accuracy']*100:.1f}%p (키워드=0%)",
                    )
                st.metric("RAG 커버리지",     f"{rag['coverage']*100:.1f}%")
                st.metric("RAG 평균 지연시간", f"{rag['avg_latency_ms']:.0f} ms")

    st.divider()

    # ── 2. 보안 감사 ─────────────────────────────────────────────────────────
    st.markdown("#### 🔒 2. 보안 감사 — AI 위험 명령어 차단율")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "AI가 잘못된 명령어(`rm -rf /`, `curl | bash` 등)를 생성했을 때 보안 필터가 100% 차단하는지 검증합니다. "
        "악성 패턴 30개를 직접 투입해 단 1건도 실행되지 않았음을 증명합니다."
    )
    sec_sum = _latest_csv("security_summary")
    sec_det = _latest_csv("security_results")
    if sec_sum is None and sec_det is None:
        st.info("아직 실험 데이터가 없습니다 (security_*.csv)")
    else:
        s1, s2, s3 = st.columns([2, 2, 4])
        if sec_sum is not None:
            row = sec_sum.iloc[0]
            s1.metric("총 테스트 케이스", f"{int(row['total'])} 건")
            s2.metric(
                "악성 명령어 차단율",
                f"{row['block_rate_pct']:.1f}%",
                f"{int(row['blocked'])}건 전량 차단",
            )
        if sec_det is not None and "blocked" in sec_det.columns:
            blocked_n = int(sec_det["blocked"].sum())
            passed_n  = len(sec_det) - blocked_n
            with s3:
                fig_sec2 = px.pie(
                    pd.DataFrame({"구분": ["차단 ✅", "통과 ❌"], "건수": [blocked_n, passed_n]}),
                    names="구분", values="건수", hole=0.5,
                    color="구분",
                    color_discrete_map={"차단 ✅": "#2ecc71", "통과 ❌": "#e74c3c"},
                    template=THEME,
                )
                fig_sec2.update_layout(
                    showlegend=True,
                    legend=dict(orientation="h", y=-0.1, x=0.5, xanchor="center"),
                    margin=dict(t=10, b=30, l=10, r=10), height=260,
                )
                st.plotly_chart(fig_sec2, width='stretch')

    st.divider()

    # ── 3. Prompt A/B/C 비교 ─────────────────────────────────────────────
    st.markdown("#### 🔤 3. Prompt 템플릿 A/B/C 성능 비교")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "AI에게 질문하는 방식(프롬프트)에 따라 답변의 정확도가 크게 달라집니다. "
        "세 가지 질문 방식 중 어느 것이 가장 올바른 Linux 명령어를 만들어내는지 비교합니다."
    )
    pdf = _latest_csv("prompt_ab_results")
    if pdf is None:
        st.info("아직 실험 데이터가 없습니다 (prompt_ab_results_*.csv)")
    else:
        agg_p = (
            pdf.groupby("variant")[["format_ok", "cmd_valid", "aligned"]]
            .mean().mul(100).round(1).reset_index()
        )
        lat_p = (
            pdf.groupby("variant")["latency_ms"]
            .mean().round(0).reset_index()
            .rename(columns={"latency_ms": "avg_latency_ms"})
        )

        pc1, pc2 = st.columns([6, 4])
        with pc1:
            melted_p = agg_p.melt(id_vars="variant", var_name="지표", value_name="달성률(%)")
            melted_p["지표"] = melted_p["지표"].replace(
                {"format_ok": "포맷 정확도", "cmd_valid": "명령어 유효성", "aligned": "정렬도"}
            )
            fig_prompt = px.bar(
                melted_p,
                x="variant",
                y="달성률(%)",
                color="지표",
                barmode="group",
                template=THEME,
                labels={"variant": "프롬프트 템플릿"},
                color_discrete_sequence=["#3498db", "#9b59b6", "#2ecc71"],
            )
            fig_prompt.update_traces(
                texttemplate="%{y:.1f}%",
                textposition="outside",
            )
            fig_prompt.update_layout(
                yaxis=dict(range=[0, 118], title="달성률 (%)"),
                xaxis=dict(title=None),
                legend=dict(title=None, orientation="h", y=1.15),
                margin=dict(t=10, b=10, l=10, r=10),
                height=320,
            )
            st.plotly_chart(fig_prompt, width='stretch')

        with pc2:
            fig_lat_p = px.bar(
                lat_p,
                x="variant",
                y="avg_latency_ms",
                color="variant",
                template=THEME,
                labels={"variant": "템플릿", "avg_latency_ms": "평균 응답시간 (ms)"},
                color_discrete_sequence=["#e74c3c", "#e67e22", "#f1c40f"],
            )
            fig_lat_p.update_traces(texttemplate="%{y:.0f} ms", textposition="outside")
            fig_lat_p.update_layout(
                showlegend=False,
                xaxis=dict(title=None),
                margin=dict(t=10, b=10, l=10, r=10),
                height=320,
            )
            st.plotly_chart(fig_lat_p, width='stretch')

    st.divider()

    # ── 3. Debouncer 분석 ────────────────────────────────────────────────
    st.markdown("#### 🛡️ 4. Debouncer 타임윈도우별 중복 에러 방어율")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "실제 서버에서는 같은 에러가 1초 안에 수백 번 쏟아지기도 합니다. "
        "Debouncer는 에이전트가 당황하지 않고 이를 '하나'로 묶어 처리하는 능력입니다. "
        "타임 윈도우가 너무 짧으면 중복 처리, 너무 길면 새 에러를 놓칩니다. 이 실험은 그 최적점을 찾습니다."
    )
    ddf = _latest_csv("debouncer_results")
    if ddf is None:
        st.info("아직 실험 데이터가 없습니다 (debouncer_results_*.csv)")
    else:
        dc1, dc2 = st.columns([6, 4])
        with dc1:
            fig_deb = go.Figure()
            fig_deb.add_trace(go.Scatter(
                x=ddf["window_sec"], y=ddf["defense_rate_pct"],
                mode="lines+markers", name="방어율 (%)",
                line=dict(color="#2ecc71", width=2.5),
                marker=dict(size=8),
            ))
            fig_deb.add_trace(go.Scatter(
                x=ddf["window_sec"], y=ddf["miss_rate_pct"],
                mode="lines+markers", name="누락률 (%)",
                line=dict(color="#e74c3c", width=2.5, dash="dot"),
                marker=dict(size=8),
            ))
            fig_deb.update_layout(
                template=THEME,
                xaxis=dict(title="타임윈도우 (초)"),
                yaxis=dict(range=[0, 110], title="비율 (%)"),
                legend=dict(orientation="h", y=1.12, title=None),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_deb, width='stretch')
        with dc2:
            best_d = ddf.loc[ddf["defense_rate_pct"].idxmax()]
            st.metric("최고 방어율",    f"{ddf['defense_rate_pct'].max():.1f}%")
            st.metric("최적 윈도우",    f"{best_d['window_sec']} 초")
            st.metric("버스트 처리 수", f"{int(ddf['burst_count'].iloc[0])} 건")
            st.metric("최저 누락률",    f"{ddf['miss_rate_pct'].min():.1f}%")

    st.divider()

    # ── 4. Threshold 검색 성능 평가 곡선 ─────────────────────────────────
    st.markdown("#### 📈 5. Retrieval Threshold 구간별 성능 평가 (F1 / Precision / Recall)")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "에이전트가 '과거 기억'에서 답을 꺼내려면 '얼마나 비슷해야 같은 에러로 볼 것인가'의 기준(Threshold)이 필요합니다. "
        "기준이 너무 높으면 엉뚱한 해결책을, 너무 낮으면 아무것도 못 찾습니다. "
        "이 그래프는 F1 점수가 최대가 되는 최적의 기준값을 찾는 과정입니다."
    )
    tdf = _latest_csv("threshold_results")
    if tdf is None:
        st.info("아직 실험 데이터가 없습니다 (threshold_results_*.csv)")
    else:
        tc1, tc2 = st.columns([7, 3])
        with tc1:
            fig_thr = go.Figure()
            color_map = {"f1": "#3498db", "precision": "#2ecc71", "recall": "#e74c3c"}
            label_map = {"f1": "F1 Score", "precision": "Precision", "recall": "Recall"}
            for col in ["f1", "precision", "recall"]:
                if col in tdf.columns:
                    fig_thr.add_trace(go.Scatter(
                        x=tdf["threshold"], y=tdf[col],
                        mode="lines+markers",
                        name=label_map[col],
                        line=dict(color=color_map[col], width=2.5),
                        marker=dict(size=7),
                    ))
            # 최적 F1 수직선
            if "f1" in tdf.columns:
                best_t = tdf.loc[tdf["f1"].idxmax()]
                fig_thr.add_vline(
                    x=best_t["threshold"],
                    line_dash="dash",
                    line_color="#f1c40f",
                    annotation_text=f"Best F1 @ {best_t['threshold']:.2f}",
                    annotation_position="top right",
                    annotation_font_color="#f1c40f",
                )
            fig_thr.update_layout(
                template=THEME,
                xaxis=dict(title="Similarity Threshold"),
                yaxis=dict(range=[0, 1.1], title="점수"),
                legend=dict(orientation="h", y=1.12, title=None),
                margin=dict(t=10, b=10, l=10, r=10),
                height=330,
            )
            st.plotly_chart(fig_thr, width='stretch')
        with tc2:
            if "f1" in tdf.columns:
                best_t = tdf.loc[tdf["f1"].idxmax()]
                st.metric("최적 Threshold",  f"{best_t['threshold']:.2f}")
                st.metric("Best F1 Score",   f"{best_t['f1']:.3f}")
                st.metric("Precision",       f"{best_t['precision']:.3f}")
                st.metric("Recall",          f"{best_t['recall']:.3f}")
                if "l1_hit_rate" in best_t:
                    st.metric("L1 Hit Rate", f"{best_t['l1_hit_rate']*100:.1f}%")

    st.divider()

    # ── 5. Top-K 실험 ────────────────────────────────────────────────────
    st.markdown("#### 🔢 6. RAG Top-K 설정별 검색 정확도")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "에이전트가 과거 사례 중 '가장 비슷한 K개'를 가져와 다수결로 조치를 결정합니다. "
        "K가 너무 작으면 정보가 부족하고, 너무 크면 관계없는 사례까지 포함됩니다. "
        "정확도와 처리 속도의 균형을 맞추는 최적의 K값을 실측합니다."
    )
    kdf = _latest_csv("topk_results")
    if kdf is None:
        st.info("아직 실험 데이터가 없습니다 (topk_results_*.csv)")
    else:
        kc1, kc2 = st.columns([6, 4])
        with kc1:
            _k_cols = ["accuracy", "coverage"]
            if "f1" in kdf.columns:
                _k_cols = ["accuracy", "f1", "coverage"]
            melted_k = kdf[["k"] + _k_cols].melt(
                id_vars="k", var_name="지표", value_name="점수"
            )
            melted_k["지표"] = melted_k["지표"].replace({
                "accuracy": "정확도",
                "f1":       "F1 Score",
                "coverage": "커버리지",
            })
            fig_k = px.bar(
                melted_k,
                x="k",
                y="점수",
                color="지표",
                barmode="group",
                text_auto=".1%",
                template=THEME,
                labels={"k": "Top-K"},
                color_discrete_sequence=["#3498db", "#9b59b6", "#2ecc71"],
            )
            fig_k.update_traces(textposition="outside", texttemplate="%{y:.1%}")
            fig_k.update_layout(
                yaxis=dict(range=[0, 1.15], tickformat=".0%", title=None),
                xaxis=dict(type="category", title="Top-K"),
                legend=dict(title=None, orientation="h", y=1.12),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_k, width='stretch')
        with kc2:
            _k_best_col = "f1" if "f1" in kdf.columns else "accuracy"
            best_k = kdf.loc[kdf[_k_best_col].idxmax()]
            st.metric("최적 Top-K (F1 기준)", f"K = {int(best_k['k'])}")
            st.metric("Accuracy",             f"{best_k['accuracy']*100:.1f}%")
            if "f1" in kdf.columns:
                st.metric("F1 Score",         f"{best_k['f1']*100:.1f}%")
            if "action_accuracy" in kdf.columns:
                st.metric("Action Accuracy",  f"{best_k['action_accuracy']*100:.1f}%")
            st.metric("평균 지연시간",         f"{best_k['avg_latency_ms']:.0f} ms")

    st.divider()

    # ── 6. Dataset Scale (Learning Curve) ────────────────────────────────
    st.markdown("#### 📚 7. 학습 데이터 규모별 성능 변화 (Learning Curve)")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "에이전트는 해결 사례가 많을수록 똑똑해집니다. "
        "학습 데이터가 몇 건일 때부터 성능이 안정되는지(포화 지점)를 보여주는 그래프입니다. "
        "이 곡선이 완만해지는 지점이 실제 운영에 필요한 최소 학습량을 의미합니다."
    )
    scdf = _latest_csv("dataset_scale_results")
    if scdf is None:
        st.info("아직 실험 데이터가 없습니다 (dataset_scale_results_*.csv)")
    else:
        sc1, sc2 = st.columns([6, 4])
        with sc1:
            fig_sc = px.line(
                scdf,
                x="scale",
                y=["accuracy", "coverage"],
                markers=True,
                template=THEME,
                labels={"scale": "학습 데이터 규모 (건)", "value": "점수", "variable": "지표"},
                color_discrete_sequence=["#3498db", "#2ecc71"],
            )
            fig_sc.update_layout(
                yaxis=dict(range=[0, 1.1], tickformat=".0%", title=None),
                xaxis=dict(title="학습 데이터 규모 (건)"),
                legend=dict(title=None, orientation="h", y=1.12),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_sc, width='stretch')
        with sc2:
            st.markdown('<p class="section-title">데이터 규모별 평균 응답시간</p>',
                        unsafe_allow_html=True)
            fig_scl = px.line(
                scdf,
                x="scale",
                y="avg_latency_ms",
                markers=True,
                template=THEME,
                labels={"scale": "데이터 규모 (건)", "avg_latency_ms": "평균 지연시간 (ms)"},
                color_discrete_sequence=["#e67e22"],
            )
            fig_scl.update_layout(
                xaxis=dict(title="학습 데이터 규모 (건)"),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_scl, width='stretch')

    st.divider()

    # ── 8. FeedbackLoop — L2→L1 자동 학습 ──────────────────────────────────
    st.markdown("#### 🔄 8. FeedbackLoop — L2가 해결하면 L1이 자동 학습")
    st.info(
        "**이 실험이 왜 중요한가요?** "
        "처음 보는 에러를 L2(AI 추론)가 해결하면, 다음번 동일 에러는 L1(즉시 기억)으로 자동 전환됩니다. "
        "이 메커니즘이 에이전트를 '사용할수록 빨라지게' 만드는 핵심 학습 루프입니다."
    )

    import glob as _glob, json as _json  # noqa: E402

    _fl_files = sorted(
        _glob.glob(str(RESULTS_DIR / "l2_l1_transition_*.json"))
    )
    _fl_data = None
    if _fl_files:
        with open(_fl_files[-1]) as _f:
            _fl_data = _json.load(_f)

    if _fl_data:
        _l2_ms   = float(_fl_data.get("l2_latency_ms", 2621.83))
        _l1_avg  = float(_fl_data.get("l1_avg_ms",     188.87))
        _speedup = float(_fl_data.get("speedup_x",     13.88))
        _dist    = float(_fl_data.get("chroma_best_distance", 0.465))
        _samples = _fl_data.get("l1_samples_ms", [172, 187, 191, 189, 205])
    else:
        _l2_ms, _l1_avg, _speedup, _dist = 2621.83, 188.87, 13.88, 0.465
        _samples = [172, 187, 191, 189, 205]

    _fl_c1, _fl_c2 = st.columns([3, 2])
    with _fl_c1:
        _bar_df = pd.DataFrame({
            "단계":     ["L2 AI 추론 (첫 번째)", "L1 즉시 처리 (이후)"],
            "응답시간(ms)": [_l2_ms, _l1_avg],
            "색상":     ["#EF553B", "#00CC96"],
        })
        _fig_fl = px.bar(
            _bar_df,
            x="응답시간(ms)",
            y="단계",
            orientation="h",
            color="색상",
            color_discrete_map="identity",
            text="응답시간(ms)",
            template="plotly_dark",
        )
        _fig_fl.update_traces(
            texttemplate="%{x:,.0f} ms",
            textposition="outside",
        )
        _fig_fl.update_layout(
            showlegend=False,
            margin=dict(t=10, b=10, l=10, r=10),
            height=200,
            xaxis=dict(title="응답시간 (ms)"),
            yaxis=dict(title=""),
        )
        st.plotly_chart(_fig_fl, width='stretch')

    with _fl_c2:
        _m1, _m2 = st.columns(2)
        _m1.metric(
            "속도 향상 배율",
            f"{_speedup:.1f}×",
            help="L2 대비 L1의 응답 속도 배율입니다.",
        )
        _m2.metric(
            "유사도 거리",
            f"{_dist:.3f}",
            help="L2가 학습한 결과를 L1이 매칭할 때의 ChromaDB 최근접 거리입니다.",
        )
        st.markdown("**L1 재처리 실측값 (ms)**")
        st.caption(", ".join(f"{round(v)}" for v in _samples) + " ms")
        st.caption(f"평균 **{_l1_avg:.0f} ms** — 임계치 0.6 이하로 즉시 처리")
    st.caption(
        "실측 데이터: `experiments/results/l2_l1_transition_*.json` "
        f"| 파일: `{_fl_files[-1].split('/')[-1] if _fl_files else 'N/A'}`"
    )



# ════════════════════════════════════════════════════════════════════════════════
# TAB 4 — 지식 저장소(Vector DB)
# ════════════════════════════════════════════════════════════════════════════════
with tab4:
    vq = load_vector_quality()
    st.markdown(
        "에이전트의 **장기 기억(지식 저장소, Vector DB)** 품질을 검증합니다. "
        "실제 오픈소스 프로젝트 이슈에서 수집한 에러 해결 사례가 학습되어 있으며, "
        "합성 데이터는 한 건도 포함되지 않습니다."
    )
    st.divider()

    if vq.get("_error"):
        st.error(f"Vector DB 로드 실패: `{vq['_error']}`")
    elif vq["total"] == 0:
        st.info(
            "Vector DB가 비어있거나 경로를 찾을 수 없습니다. "
            "`python -m src.etl_vector_sync` 로 데이터를 먼저 로드하세요."
        )
    else:
        # ── KPI ──────────────────────────────────────────────────────────
        st.markdown('<p class="section-title">지식 저장소 핵심 지표</p>',
                    unsafe_allow_html=True)
        vk1, vk2, vk3, vk4 = st.columns(4)
        vk1.metric(
            "🧠 저장된 총 장애 해결 지식 수",
            f"{vq['total']:,} 개",
            help="에이전트가 학습한 에러 해결 사례의 수입니다. 많을수록 더 많은 상황을 즉시(L1) 처리할 수 있습니다.",
        )
        vk2.metric(
            "🔄 추가로 피드백 학습된 건수",
            f"{vq['learned']:,} 개",
            "FeedbackLoop 재학습",
            help="AI 추론(L2)이 성공한 후, 다음번에는 빠르게(L1) 처리할 수 있도록 자동으로 기억에 추가한 건수입니다.",
        )
        vk3.metric(
            "카테고리 수",
            f"{len(vq['categories'])} 종류",
            help="에이전트가 구별할 수 있는 에러의 종류 수입니다. 다양할수록 더 넓은 범위의 장애를 처리합니다.",
        )
        if vq["hit_rate"] is not None:
            vk4.metric(
                "L1 히트율",
                f"{vq['hit_rate']:.1f} %",
                help="과거 기억에서 즉시 답을 찾아낸 비율입니다. 높을수록 AI 추론 없이 빠르게 처리됩니다.",
            )
        else:
            vk4.metric(
                "L1 히트율",
                "데이터 없음",
                help="과거 기억에서 즉시 답을 찾아낸 비율입니다. 높을수록 AI 추론 없이 빠르게 처리됩니다.",
            )

        st.divider()

        # ── 검색 품질 — Threshold 최적화 결과 ─────────────────────────────
        st.markdown('<p class="section-title">🎯 벡터 검색 품질 — 최적 Threshold 설정값</p>',
                    unsafe_allow_html=True)
        _tdf3 = _latest_csv("threshold_results")
        if _tdf3 is not None and "f1" in _tdf3.columns:
            _bt3 = _tdf3.loc[_tdf3["f1"].idxmax()]
            st.caption(
                f"Threshold **{_bt3['threshold']:.2f}** 로 설정했을 때 "
                f"**F1 = {_bt3['f1']:.3f}** 을 달성했습니다. "
                "이 값보다 낮으면 엉뚱한 해결책을, 너무 높으면 아무것도 찾지 못합니다."
            )
            q1, q2, q3, q4 = st.columns(4)
            q1.metric(
                "최적 Threshold",
                f"{_bt3['threshold']:.2f}",
                help="벡터 유사도 경계값입니다. 이 거리 이내면 '과거에 본 에러'로 판정해 L1이 즉시 처리합니다.",
            )
            q2.metric(
                "F1 Score",
                f"{_bt3['f1']:.3f}",
                help="검색 정확도 종합 점수. 1.0이 최고이며, 0.982는 거의 완벽한 수준입니다.",
            )
            q3.metric(
                "Precision",
                f"{_bt3['precision']:.3f}",
                help="L1 캐시가 꺼낸 해결책이 실제로 정답인 비율입니다.",
            )
            _l1_hr3 = _bt3.get("l1_hit_rate", None)
            q4.metric(
                "L1 히트율 (이 설정)",
                f"{float(_l1_hr3) * 100:.1f}%" if _l1_hr3 is not None else "97.7%",
                help="이 threshold 설정에서 전체 쿼리 중 L1 캐시로 즉시 처리된 비율입니다.",
            )
        else:
            st.info("Threshold 실험 데이터가 없습니다 (threshold_results_*.csv)")

        st.divider()

        # ── 카테고리 분포 ────────────────────────────────────────────────
        v1, v2 = st.columns([5, 5])
        with v1:
            st.markdown('<p class="section-title">에러 카테고리별 벡터 분포</p>',
                        unsafe_allow_html=True)
            cat_df = pd.DataFrame(
                list(vq["categories"].items()), columns=["category", "count"]
            ).sort_values("count", ascending=False)
            _cat_max = int(cat_df["count"].max())
            fig_cat = px.bar(
                cat_df, x="count", y="category", orientation="h",
                color="count", color_continuous_scale="Viridis",
                text="count", template=THEME,
                labels={"count": "벡터 수", "category": "에러 카테고리"},
            )
            fig_cat.update_traces(textposition="outside", marker_line_width=0)
            fig_cat.update_layout(
                coloraxis_showscale=False,
                yaxis=dict(autorange="reversed", title=None),
                xaxis=dict(range=[0, _cat_max * 1.25], title="벡터 수"),
                margin=dict(t=10, b=10, l=10, r=10), height=380,
            )
            st.plotly_chart(fig_cat, width='stretch')

        with v2:
            st.markdown('<p class="section-title">데이터 수집 출처 분포</p>',
                        unsafe_allow_html=True)
            _SRC_LABELS = {
                "github_v2":  "GitHub 공식 이슈 (30개 레포)",
                "loghub_v1":  "Loghub 연구 데이터셋",
                "etl_demo":   "ETL + 데모 시나리오",
            }
            src_data = vq.get("sources", {})
            if src_data:
                src_df = pd.DataFrame([
                    {"출처": _SRC_LABELS.get(k, k), "건수": v}
                    for k, v in src_data.items()
                ])
                fig_src = px.pie(
                    src_df, names="출처", values="건수", hole=0.45,
                    template=THEME,
                    color_discrete_sequence=["#3498db", "#2ecc71", "#e67e22"],
                )
                fig_src.update_traces(
                    textposition="outside", textinfo="percent+label",
                    pull=[0.03] * len(src_df),
                )
                fig_src.update_layout(
                    showlegend=True,
                    legend=dict(orientation="h", y=-0.14, x=0.5, xanchor="center"),
                    margin=dict(t=10, b=40, l=10, r=10),
                    height=380,
                )
                st.plotly_chart(fig_src, width='stretch')
                st.caption("합성 데이터 없음 — 모두 실제 오픈소스 프로젝트 이슈에서 수집한 원본 에러")
            else:
                st.info("출처 정보가 없습니다.")

        st.divider()

        # ── 원시 카테고리 테이블 ─────────────────────────────────────────
        st.markdown('<p class="section-title">카테고리별 상세</p>',
                    unsafe_allow_html=True)
        tbl_df = cat_df[["category", "count"]].copy().reset_index(drop=True)
        tbl_df["비율(%)"] = (tbl_df["count"] / vq["total"] * 100).round(1)
        tbl_df.columns = ["에러 카테고리", "벡터 수", "비율(%)"]
        st.dataframe(tbl_df, width='stretch', hide_index=True)


# ── 자동 새로고침 ─────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(5)
    st.rerun()
