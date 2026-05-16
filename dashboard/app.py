

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
        font-size: 0.78rem;
        font-weight: 700;
        color: #7f8ea8;
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


# ── 데이터 로드 ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def load_metrics() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query("SELECT * FROM metrics ORDER BY timestamp DESC", conn)
        if df.empty:
            return df
        df["timestamp"]       = pd.to_datetime(df["timestamp"])
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

    auto_refresh = st.toggle("🔄 자동 새로고침 (30초)", value=False)
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
    """ChromaDB에서 벡터 품질 지표를 수집한다."""
    result = {
        "total": 0, "categories": {}, "learned": 0,
        "hit_rate": None, "dead_count": 0,
        "_error": None,
    }
    if not CHROMA_PATH.exists():
        result["_error"] = f"경로 없음: {CHROMA_PATH}"
        return result
    try:
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(
            path=str(CHROMA_PATH),
            settings=Settings(anonymized_telemetry=False),
        )
        col = client.get_collection("error_playbook_vectors")
        data = col.get(include=["metadatas"])
        metas = data["metadatas"]
        result["total"] = len(metas)
        from collections import Counter
        cats = Counter(m.get("error_category", "Unknown") for m in metas)
        result["categories"] = dict(cats)
        result["learned"] = sum(1 for m in metas if m.get("learned_at"))
    except Exception as e:
        result["_error"] = str(e)

    # metrics DB에서 L1 히트율 계산
    if DB_PATH.exists() and result["total"] > 0:
        try:
            import sqlite3
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
tab1, tab2, tab3 = st.tabs([
    "📡  실시간 에이전트 모니터링",
    "🔬  아키텍처 실험 및 성능 평가",
    "🧬  Vector DB 품질 모니터링",
])


# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — 실시간 에이전트 모니터링
# ════════════════════════════════════════════════════════════════════════════════
with tab1:
    if df.empty:
        st.info(
            "📊 아직 수집된 메트릭 데이터가 없습니다. "
            "Agent가 에러를 처리하면 여기에 자동으로 표시됩니다."
        )
    else:
        total   = len(df)
        success = int(df["success"].astype(int).sum())
        s_rate  = success / total * 100
        l1_rate = (df["resolution_source"] == "L1_CACHE").mean() * 100
        avg_ms  = df["latency_ms"].mean()

        prev_s_rate = (df_prev["success"].astype(int).mean() * 100) if not df_prev.empty else None
        prev_l1     = ((df_prev["resolution_source"] == "L1_CACHE").mean() * 100) if not df_prev.empty else None
        prev_ms     = (df_prev["latency_ms"].mean()) if not df_prev.empty else None

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

        # ── KPI 4개 ──────────────────────────────────────────────────────
        st.markdown('<p class="section-title">Key Performance Indicators</p>',
                    unsafe_allow_html=True)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("총 발생 에러 수",  f"{total:,} 건")
        k2.metric(
            "SUCCESS 비율",
            f"{s_rate:.1f} %",
            f"{s_rate - prev_s_rate:+.1f}%p" if prev_s_rate is not None else None,
        )
        k3.metric(
            "L1 Cache 적중률",
            f"{l1_rate:.1f} %",
            f"{l1_rate - prev_l1:+.1f}%p" if prev_l1 is not None else None,
        )
        k4.metric(
            "평균 조치 소요 시간",
            f"{avg_ms:.0f} ms",
            f"{avg_ms - prev_ms:+.0f} ms" if prev_ms is not None else None,
            delta_color="inverse",
        )

        st.divider()

        # ── MTTR 비교 (수동 vs 에이전트) ────────────────────────────────
        st.markdown('<p class="section-title">MTTR — 평균 장애 복구 시간 비교 (수동 On-Call vs 에이전트 자동복구)</p>',
                    unsafe_allow_html=True)

        # 수동 기준: 온콜 페이징(5분) + 로그인/이동(3분) + 진단(15분) + 조치(7분) = 30분
        MANUAL_MTTR_SEC = 1800.0
        agent_mttr_sec  = df["latency_sec"].mean()
        reduction_pct   = (1 - agent_mttr_sec / MANUAL_MTTR_SEC) * 100
        l1_avg_ms = df[df["resolution_source"] == "L1_CACHE"]["latency_sec"].mean() * 1000
        l2_mask   = df["resolution_source"] == "L2_LLM"
        l2_avg_ms = df[l2_mask]["latency_sec"].mean() * 1000 if l2_mask.any() else None

        mc1, mc2 = st.columns([6, 4])

        with mc1:
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
            st.plotly_chart(fig_mttr, use_container_width=True)

        with mc2:
            st.metric(
                "MTTR 단축률",
                f"{reduction_pct:.1f} %",
                f"1,800초 → {agent_mttr_sec * 1000:.0f} ms",
            )
            st.caption(f"⚡ L1 Cache 평균: {l1_avg_ms:.0f} ms")
            if l2_avg_ms is not None:
                st.caption(f"🧠 L2 LLM 평균: {l2_avg_ms:.0f} ms")
            st.caption("※ 수동 기준: 온콜 페이징+진단+조치 30분 (업계 평균)")

        st.divider()

        # ── 시각화 Row 1: 도넛 차트 + 에러 타입 바 차트 ─────────────────
        v1, v2 = st.columns([4, 6])

        with v1:
            st.markdown('<p class="section-title">결과 분류 비율</p>',
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
            st.plotly_chart(fig_donut, use_container_width=True)

        with v2:
            st.markdown('<p class="section-title">에러 카테고리별 발생 빈도</p>',
                        unsafe_allow_html=True)
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
            st.plotly_chart(fig_etype, use_container_width=True)

        st.divider()

        # ── 시각화 Row 2: 추론 속도 타임라인 + 해결 소스별 누적 막대 ─────
        v3, v4 = st.columns([6, 4])

        with v3:
            st.markdown('<p class="section-title">추론 속도 타임라인 (L1 vs L2)</p>',
                        unsafe_allow_html=True)
            scatter_df = df[["timestamp", "latency_ms", "resolution_source"]].dropna()
            fig_scatter = px.scatter(
                scatter_df,
                x="timestamp",
                y="latency_ms",
                color="resolution_source",
                color_discrete_map=SOURCE_COLORS,
                opacity=0.72,
                template=THEME,
                labels={
                    "timestamp":         "시간",
                    "latency_ms":        "지연시간 (ms)",
                    "resolution_source": "해결 소스",
                },
            )
            fig_scatter.update_layout(
                legend=dict(title=None, orientation="h", y=1.1),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

        with v4:
            st.markdown('<p class="section-title">해결 소스별 결과 분류</p>',
                        unsafe_allow_html=True)
            stacked = (
                df.groupby(["resolution_source", "result_category"])
                .size()
                .reset_index(name="count")
            )
            fig_stk = px.bar(
                stacked,
                x="resolution_source",
                y="count",
                color="result_category",
                color_discrete_map=RESULT_COLORS,
                barmode="stack",
                template=THEME,
                labels={
                    "resolution_source": "해결 소스",
                    "count":             "건수",
                    "result_category":   "결과",
                },
            )
            fig_stk.update_layout(
                legend=dict(title=None, orientation="h", y=1.1),
                xaxis=dict(title=None),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_stk, use_container_width=True)

        st.divider()

        # ── 최근 에러 로그 테이블 (10건) ─────────────────────────────────
        st.markdown('<p class="section-title">최근 에러 처리 로그 (10건)</p>',
                    unsafe_allow_html=True)

        _SHOW  = ["timestamp", "error_category", "result_category",
                  "resolution_source", "action_type", "latency_ms", "error_log"]
        _SHOW  = [c for c in _SHOW if c in df.columns]
        log_df = df[_SHOW].head(10).copy()

        log_df["resolution_source"] = log_df["resolution_source"].replace(
            {"L1_CACHE": "⚡ L1 (Cache)", "L2_LLM": "🧠 L2 (LLM)"}
        )
        log_df["result_category"] = log_df["result_category"].replace(
            {"SUCCESS": "✅ SUCCESS", "FAILURE": "⚠️ FAILURE", "IMPOSSIBLE": "🚫 IMPOSSIBLE"}
        )

        _RENAME = {
            "timestamp":         "발생 시간",
            "error_category":    "에러 카테고리",
            "result_category":   "결과 분류",
            "resolution_source": "해결 소스",
            "action_type":       "실행 커맨드",
            "latency_ms":        "소요(ms)",
            "error_log":         "에러 원문",
        }
        log_df = log_df.rename(columns=_RENAME)

        st.dataframe(
            log_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "소요(ms)":  st.column_config.NumberColumn(format="%.1f ms"),
                "발생 시간": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
                "에러 원문": st.column_config.TextColumn(width="large"),
            },
        )


# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — 아키텍처 실험 및 성능 평가
# ════════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown(
        "📂 `experiments/results/` 폴더에서 **가장 최신 실험 결과 CSV**를 자동으로 로드합니다."
    )
    st.divider()

    # ── 1. Baseline vs RAG ───────────────────────────────────────────────
    st.markdown("#### 🏆 1. Baseline vs RAG 시스템 성능 비교")
    bdf = _latest_csv("baseline_results")
    if bdf is None:
        st.info("아직 실험 데이터가 없습니다 (baseline_results_*.csv)")
    else:
        bc1, bc2 = st.columns([6, 4])
        with bc1:
            melted_b = bdf[["system", "accuracy", "coverage"]].melt(
                id_vars="system", var_name="지표", value_name="점수"
            )
            fig_bl = px.bar(
                melted_b,
                x="system",
                y="점수",
                color="지표",
                barmode="group",
                text_auto=".1%",
                template=THEME,
                labels={"system": "시스템"},
                color_discrete_sequence=["#3498db", "#2ecc71"],
            )
            fig_bl.update_traces(textposition="outside", texttemplate="%{y:.1%}")
            fig_bl.update_layout(
                yaxis=dict(range=[0, 1.15], tickformat=".0%", title=None),
                xaxis=dict(title=None),
                legend=dict(title=None, orientation="h", y=1.12),
                margin=dict(t=10, b=10, l=10, r=10),
                height=340,
            )
            st.plotly_chart(fig_bl, use_container_width=True)
        with bc2:
            if len(bdf) >= 2:
                kw  = bdf.iloc[0]
                rag = bdf.iloc[1]
                st.metric("키워드 기반 정확도", f"{kw['accuracy']*100:.1f}%")
                st.metric(
                    "RAG 시스템 정확도",
                    f"{rag['accuracy']*100:.1f}%",
                    f"+{(rag['accuracy'] - kw['accuracy'])*100:.1f}%p 향상",
                )
                st.metric("RAG 커버리지",     f"{rag['coverage']*100:.1f}%")
                st.metric("RAG 평균 지연시간", f"{rag['avg_latency_ms']:.0f} ms")

    st.divider()

    # ── 2. Prompt A/B/C 비교 ─────────────────────────────────────────────
    st.markdown("#### 🔤 2. Prompt 템플릿 A/B/C 성능 비교")
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
            st.plotly_chart(fig_prompt, use_container_width=True)

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
            st.plotly_chart(fig_lat_p, use_container_width=True)

    st.divider()

    # ── 3. Debouncer 분석 ────────────────────────────────────────────────
    st.markdown("#### 🛡️ 3. Debouncer 타임윈도우별 중복 에러 방어율")
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
            st.plotly_chart(fig_deb, use_container_width=True)
        with dc2:
            best_d = ddf.loc[ddf["defense_rate_pct"].idxmax()]
            st.metric("최고 방어율",    f"{ddf['defense_rate_pct'].max():.1f}%")
            st.metric("최적 윈도우",    f"{best_d['window_sec']} 초")
            st.metric("버스트 처리 수", f"{int(ddf['burst_count'].iloc[0])} 건")
            st.metric("최저 누락률",    f"{ddf['miss_rate_pct'].min():.1f}%")

    st.divider()

    # ── 4. Threshold 검색 성능 평가 곡선 ─────────────────────────────────
    st.markdown("#### 📈 4. Retrieval Threshold 구간별 성능 평가 (F1 / Precision / Recall)")
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
            st.plotly_chart(fig_thr, use_container_width=True)
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
    st.markdown("#### 🔢 5. RAG Top-K 설정별 검색 정확도")
    kdf = _latest_csv("topk_results")
    if kdf is None:
        st.info("아직 실험 데이터가 없습니다 (topk_results_*.csv)")
    else:
        kc1, kc2 = st.columns([6, 4])
        with kc1:
            melted_k = kdf[["k", "accuracy", "coverage"]].melt(
                id_vars="k", var_name="지표", value_name="점수"
            )
            fig_k = px.bar(
                melted_k,
                x="k",
                y="점수",
                color="지표",
                barmode="group",
                text_auto=".1%",
                template=THEME,
                labels={"k": "Top-K"},
                color_discrete_sequence=["#3498db", "#2ecc71"],
            )
            fig_k.update_traces(textposition="outside", texttemplate="%{y:.1%}")
            fig_k.update_layout(
                yaxis=dict(range=[0, 1.15], tickformat=".0%", title=None),
                xaxis=dict(type="category", title="Top-K"),
                legend=dict(title=None, orientation="h", y=1.12),
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig_k, use_container_width=True)
        with kc2:
            best_k = kdf.loc[kdf["accuracy"].idxmax()]
            st.metric("최고 정확도 Top-K", f"K = {int(best_k['k'])}")
            st.metric("Accuracy",          f"{best_k['accuracy']*100:.1f}%")
            st.metric("Coverage",          f"{best_k['coverage']*100:.1f}%")
            st.metric("평균 지연시간",      f"{best_k['avg_latency_ms']:.0f} ms")

    st.divider()

    # ── 6. Dataset Scale (Learning Curve) ────────────────────────────────
    st.markdown("#### 📚 6. 학습 데이터 규모별 성능 변화 (Learning Curve)")
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
            st.plotly_chart(fig_sc, use_container_width=True)
        with sc2:
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
            st.plotly_chart(fig_scl, use_container_width=True)

    st.divider()

    # ── 7. 보안 감사 ─────────────────────────────────────────────────────
    st.markdown("#### 🔒 7. 보안 감사 (Malicious Command Block Audit)")
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
                "Block Rate",
                f"{row['block_rate_pct']:.1f}%",
                f"{int(row['blocked'])}건 차단",
            )
        if sec_det is not None and "blocked" in sec_det.columns:
            blocked_n = int(sec_det["blocked"].sum())
            passed_n  = len(sec_det) - blocked_n
            with s3:
                fig_sec = px.pie(
                    pd.DataFrame({"구분": ["차단", "통과"], "건수": [blocked_n, passed_n]}),
                    names="구분",
                    values="건수",
                    hole=0.5,
                    color="구분",
                    color_discrete_map={"차단": "#2ecc71", "통과": "#e74c3c"},
                    template=THEME,
                )
                fig_sec.update_layout(
                    showlegend=True,
                    legend=dict(orientation="h", y=-0.1, x=0.5, xanchor="center"),
                    margin=dict(t=10, b=30, l=10, r=10),
                    height=260,
                )
                st.plotly_chart(fig_sec, use_container_width=True)


# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — Vector DB 품질 모니터링
# ════════════════════════════════════════════════════════════════════════════════
with tab3:
    vq = load_vector_quality()
    st.markdown("ChromaDB에 누적된 에러 지식 벡터의 품질과 분포를 모니터링합니다.")
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
        st.markdown('<p class="section-title">Vector DB 핵심 지표</p>',
                    unsafe_allow_html=True)
        vk1, vk2, vk3, vk4 = st.columns(4)
        vk1.metric("총 벡터 수",       f"{vq['total']:,} 개")
        vk2.metric("학습된 벡터 (L2)", f"{vq['learned']:,} 개",
                   f"{vq['learned']/vq['total']*100:.1f}%")
        vk3.metric("카테고리 수",      f"{len(vq['categories'])} 종류")
        if vq["hit_rate"] is not None:
            vk4.metric("L1 히트율",    f"{vq['hit_rate']:.1f} %")
        else:
            vk4.metric("L1 히트율",    "데이터 없음")

        st.divider()

        # ── 카테고리 분포 ────────────────────────────────────────────────
        v1, v2 = st.columns([5, 5])
        with v1:
            st.markdown('<p class="section-title">에러 카테고리별 벡터 분포</p>',
                        unsafe_allow_html=True)
            cat_df = pd.DataFrame(
                list(vq["categories"].items()), columns=["category", "count"]
            ).sort_values("count", ascending=False)
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
                margin=dict(t=10, b=10, l=10, r=60), height=380,
            )
            st.plotly_chart(fig_cat, use_container_width=True)

        with v2:
            st.markdown('<p class="section-title">벡터 구성 비율 (원본 vs 학습)</p>',
                        unsafe_allow_html=True)
            original = vq["total"] - vq["learned"]
            fig_pie = px.pie(
                pd.DataFrame({
                    "구분": ["원본 Playbook", "L2/Rule 학습"],
                    "수":   [original, vq["learned"]],
                }),
                names="구분", values="수", hole=0.5,
                color_discrete_sequence=["#3498db", "#2ecc71"],
                template=THEME,
            )
            fig_pie.update_layout(
                legend=dict(orientation="h", y=-0.1, x=0.5, xanchor="center"),
                margin=dict(t=10, b=30, l=10, r=10), height=380,
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        st.divider()

        # ── 원시 카테고리 테이블 ─────────────────────────────────────────
        st.markdown('<p class="section-title">카테고리별 상세</p>',
                    unsafe_allow_html=True)
        cat_df["비율(%)"] = (cat_df["count"] / vq["total"] * 100).round(1)
        cat_df.columns = ["에러 카테고리", "벡터 수", "비율(%)"]
        st.dataframe(cat_df, use_container_width=True, hide_index=True)


# ── 자동 새로고침 ─────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(30)
    st.rerun()
