"""
평가 그래프 생성 스크립트 — 발표·논문 Evaluation 챕터용

생성 파일 (experiments/results/charts/):
  01_l2_vs_l1_latency.png   — L2 LLM vs L1 Cache 처리 속도 비교
  02_mttr_comparison.png    — 수동 On-Call vs 에이전트 MTTR
  03_success_by_category.png— 에러 카테고리별 성공률
  04_result_distribution.png— SUCCESS / FAILURE / IMPOSSIBLE 비율
  05_latency_distribution.png — L1 응답 속도 분포 (히스토그램)

실행: python -m experiments.generate_eval_charts
"""

import json
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
DB_PATH     = BASE_DIR / "data" / "agent_metrics.db"
RESULTS_DIR = Path(__file__).parent / "results"
CHART_DIR   = RESULTS_DIR / "charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)

# ── 색상 팔레트 ───────────────────────────────────────────────────────────────
C_SUCCESS  = "#2ecc71"
C_FAILURE  = "#e74c3c"
C_IMPOSSIBLE = "#e67e22"
C_L1       = "#3498db"
C_L2       = "#9b59b6"
C_MANUAL   = "#e74c3c"
C_AGENT    = "#2ecc71"
BG         = "#1a1f2e"
GRID       = "#2d3748"
TEXT       = "#e8eaf0"

plt.rcParams.update({
    "figure.facecolor":  BG,
    "axes.facecolor":    BG,
    "axes.edgecolor":    GRID,
    "axes.labelcolor":   TEXT,
    "xtick.color":       TEXT,
    "ytick.color":       TEXT,
    "text.color":        TEXT,
    "grid.color":        GRID,
    "grid.alpha":        0.5,
    "font.family":       ["NanumBarunGothic", "NanumGothic", "DejaVu Sans"],
    "font.size":         11,
})


def load_db() -> list[dict]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM metrics ORDER BY timestamp ASC").fetchall()
    return [dict(r) for r in rows]


def load_l2_l1_result() -> dict | None:
    files = sorted(RESULTS_DIR.glob("l2_l1_transition_*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 1 — L2 vs L1 처리 속도 비교 (핵심 그래프)
# ═══════════════════════════════════════════════════════════════════════════════
def chart_l2_vs_l1():
    data = load_l2_l1_result()
    if data is None:
        print("  ⚠️  l2_l1_transition_*.json 없음. 스킵.")
        return

    l2_ms  = data["l2_latency_ms"]
    l1_avg = data["l1_avg_ms"]
    l1_samples = data.get("l1_samples_ms", [l1_avg])
    speedup    = data["speedup_x"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle("L2 (LLM) vs L1 (Cache) 처리 속도 비교", fontsize=15, fontweight="bold",
                 color=TEXT, y=1.01)

    # ── 왼쪽: 막대 비교 ──────────────────────────────────────────────
    ax = axes[0]
    bars = ax.bar(
        ["L2\n(처음 겪는 에러\nLLM 추론)", "L1\n(학습 후\nCache 조회)"],
        [l2_ms, l1_avg],
        color=[C_L2, C_L1],
        width=0.5,
        edgecolor="none",
    )
    # 값 레이블
    for bar, val in zip(bars, [l2_ms, l1_avg]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 30,
                f"{val:.0f} ms", ha="center", va="bottom",
                fontsize=13, fontweight="bold", color=TEXT)

    ax.set_ylabel("처리 소요 시간 (ms)", labelpad=8)
    ax.set_title(f"속도 향상: {speedup:.1f}×", fontsize=13, color=C_L1, pad=8)
    ax.yaxis.grid(True, linestyle="--")
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)

    # 주석: 자가 학습 효과
    ax.annotate(
        f"자가 학습 후\n{speedup:.1f}× 빠름",
        xy=(1, l1_avg), xytext=(0.6, l2_ms * 0.6),
        arrowprops=dict(arrowstyle="->", color=C_SUCCESS, lw=1.8),
        fontsize=10, color=C_SUCCESS,
    )

    # ── 오른쪽: L1 반복 측정 안정성 ─────────────────────────────────
    ax2 = axes[1]
    x_labels = [f"{i+1}차" for i in range(len(l1_samples))]
    ax2.plot(x_labels, l1_samples, "o-", color=C_L1, linewidth=2.5, markersize=9,
             label="L1 측정값")
    ax2.axhline(l1_avg, linestyle="--", color=C_SUCCESS, linewidth=1.8,
                label=f"평균 {l1_avg:.0f} ms")
    ax2.axhline(l2_ms,  linestyle=":",  color=C_L2,     linewidth=1.8,
                label=f"L2 기준 {l2_ms:.0f} ms")

    ax2.fill_between(range(len(l1_samples)),
                     [v * 0.9 for v in l1_samples],
                     [v * 1.1 for v in l1_samples],
                     color=C_L1, alpha=0.15)

    ax2.set_ylabel("처리 소요 시간 (ms)", labelpad=8)
    ax2.set_title("L1 Cache 반복 측정 안정성", fontsize=13, pad=8)
    ax2.legend(loc="upper right", fontsize=9, framealpha=0.3)
    ax2.yaxis.grid(True, linestyle="--")
    ax2.set_axisbelow(True)
    ax2.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    out = CHART_DIR / "01_l2_vs_l1_latency.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 2 — MTTR 비교
# ═══════════════════════════════════════════════════════════════════════════════
def chart_mttr(rows: list[dict]):
    if not rows:
        return

    avg_ms = sum(r["latency_sec"] * 1000 for r in rows) / len(rows)
    manual_ms = 1_800_000.0  # 30분
    reduction = (1 - avg_ms / manual_ms) * 100
    speedup   = manual_ms / avg_ms

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.suptitle("MTTR (평균 장애 복구 시간) 비교", fontsize=15, fontweight="bold",
                 color=TEXT)

    categories = ["수동 On-Call 복구\n(온콜 페이징 → 진단 → 조치)", "에이전트 자동 복구\n(감지 → 분석 → 실행)"]
    values     = [manual_ms, avg_ms]
    colors     = [C_MANUAL, C_AGENT]

    bars = ax.barh(categories, values, color=colors, height=0.4, edgecolor="none")

    # 값 레이블
    labels = ["30분 (1,800,000 ms)", f"{avg_ms:.0f} ms"]
    for bar, label in zip(bars, labels):
        ax.text(bar.get_width() + manual_ms * 0.01, bar.get_y() + bar.get_height() / 2,
                label, va="center", fontsize=12, fontweight="bold", color=TEXT)

    ax.set_xscale("log")
    ax.set_xlabel("복구 소요 시간 (ms, 로그 스케일)")

    # 단축률 주석
    ax.set_title(
        f"MTTR {reduction:.1f}% 단축  ·  {speedup:,.0f}× 빠름",
        fontsize=13, color=C_AGENT, pad=8,
    )
    ax.xaxis.grid(True, linestyle="--")
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    out = CHART_DIR / "02_mttr_comparison.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 3 — 에러 카테고리별 성공률 + 평균 처리 시간
# ═══════════════════════════════════════════════════════════════════════════════
def chart_success_by_category(rows: list[dict]):
    if not rows:
        return

    from collections import defaultdict
    cat_data: dict[str, dict] = defaultdict(lambda: {"total": 0, "success": 0, "latency_ms": []})

    for r in rows:
        cat = r.get("error_category") or "Unknown"
        cat_data[cat]["total"] += 1
        if r["result_category"] == "SUCCESS":
            cat_data[cat]["success"] += 1
        cat_data[cat]["latency_ms"].append(r["latency_sec"] * 1000)

    categories = sorted(cat_data, key=lambda c: -cat_data[c]["total"])
    success_rates = [cat_data[c]["success"] / cat_data[c]["total"] * 100 for c in categories]
    avg_latencies  = [sum(cat_data[c]["latency_ms"]) / len(cat_data[c]["latency_ms"]) for c in categories]
    totals         = [cat_data[c]["total"] for c in categories]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("에러 카테고리별 성능 분석", fontsize=15, fontweight="bold", color=TEXT, y=1.01)

    # ── 성공률 ───────────────────────────────────────────────────────
    bar_colors = [C_SUCCESS if s >= 90 else C_FAILURE if s < 50 else C_IMPOSSIBLE
                  for s in success_rates]
    bars = ax1.barh(categories, success_rates, color=bar_colors, height=0.6, edgecolor="none")
    for bar, rate, total in zip(bars, success_rates, totals):
        ax1.text(min(rate + 1, 99), bar.get_y() + bar.get_height() / 2,
                 f"{rate:.0f}%  (n={total})", va="center", fontsize=10, color=TEXT)
    ax1.axvline(100, linestyle="--", color=GRID, linewidth=1)
    ax1.set_xlim(0, 120)
    ax1.set_xlabel("성공률 (%)")
    ax1.set_title("카테고리별 성공률", fontsize=13)
    ax1.xaxis.grid(True, linestyle="--")
    ax1.set_axisbelow(True)
    ax1.spines[["top", "right"]].set_visible(False)

    # ── 평균 처리 시간 ───────────────────────────────────────────────
    bars2 = ax2.barh(categories, avg_latencies, color=C_L1, height=0.6, edgecolor="none")
    for bar, lat in zip(bars2, avg_latencies):
        ax2.text(lat + 10, bar.get_y() + bar.get_height() / 2,
                 f"{lat:.0f} ms", va="center", fontsize=10, color=TEXT)
    ax2.set_xlabel("평균 처리 시간 (ms)")
    ax2.set_title("카테고리별 평균 복구 속도", fontsize=13)
    ax2.xaxis.grid(True, linestyle="--")
    ax2.set_axisbelow(True)
    ax2.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    out = CHART_DIR / "03_success_by_category.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 4 — 결과 분류 비율 도넛
# ═══════════════════════════════════════════════════════════════════════════════
def chart_result_distribution(rows: list[dict]):
    if not rows:
        return

    from collections import Counter
    counts = Counter(r["result_category"] for r in rows)
    labels  = list(counts.keys())
    values  = list(counts.values())
    colors  = [C_SUCCESS if l == "SUCCESS" else C_FAILURE if l == "FAILURE" else C_IMPOSSIBLE
               for l in labels]
    total   = sum(values)

    fig, ax = plt.subplots(figsize=(7, 6))
    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops={"width": 0.55, "edgecolor": BG, "linewidth": 2},
        pctdistance=0.78,
    )
    for at in autotexts:
        at.set_fontsize(12)
        at.set_fontweight("bold")
        at.set_color(BG)

    # 중앙 텍스트
    ax.text(0, 0.12, f"{total}", ha="center", va="center",
            fontsize=26, fontweight="bold", color=TEXT)
    ax.text(0, -0.18, "총 처리 건수", ha="center", va="center",
            fontsize=11, color=TEXT)

    patches = [mpatches.Patch(color=c, label=f"{l} ({v}건)")
               for l, v, c in zip(labels, values, colors)]
    ax.legend(handles=patches, loc="lower center", bbox_to_anchor=(0.5, -0.08),
              ncol=3, fontsize=10, framealpha=0.2)

    ax.set_title("에이전트 조치 결과 분류", fontsize=14, fontweight="bold", pad=16)

    plt.tight_layout()
    out = CHART_DIR / "04_result_distribution.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 5 — L1 응답 속도 분포 히스토그램
# ═══════════════════════════════════════════════════════════════════════════════
def chart_latency_distribution(rows: list[dict]):
    if not rows:
        return

    latencies_ms = [r["latency_sec"] * 1000 for r in rows]
    p50 = np.percentile(latencies_ms, 50)
    p90 = np.percentile(latencies_ms, 90)
    p99 = np.percentile(latencies_ms, 99)

    fig, ax = plt.subplots(figsize=(10, 5))

    n, bins, patches = ax.hist(latencies_ms, bins=30, color=C_L1, edgecolor=BG,
                                linewidth=0.8, alpha=0.85)
    # 구간 색상 구분
    for patch, left in zip(patches, bins[:-1]):
        if left < 100:
            patch.set_facecolor(C_SUCCESS)
        elif left < 500:
            patch.set_facecolor(C_L1)
        else:
            patch.set_facecolor(C_FAILURE)

    # 퍼센타일 선
    for pct, val, style in [(50, p50, "--"), (90, p90, "-."), (99, p99, ":")]:
        ax.axvline(val, linestyle=style, color="white", linewidth=1.8,
                   label=f"P{pct}: {val:.0f} ms")

    ax.set_xlabel("처리 소요 시간 (ms)")
    ax.set_ylabel("빈도 (건수)")
    ax.set_title("에이전트 처리 속도 분포 (L1 Cache)", fontsize=14, fontweight="bold")
    ax.legend(fontsize=10, framealpha=0.3)
    ax.yaxis.grid(True, linestyle="--")
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)

    # 범례 박스
    ax.text(0.98, 0.95,
            f"총 {len(latencies_ms)}건\n평균 {np.mean(latencies_ms):.0f} ms\n중앙값 {p50:.0f} ms",
            transform=ax.transAxes, va="top", ha="right",
            fontsize=10, color=TEXT,
            bbox=dict(boxstyle="round,pad=0.5", facecolor=GRID, alpha=0.6))

    plt.tight_layout()
    out = CHART_DIR / "05_latency_distribution.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 6 — OOM(VRAM) 방어 성공 현황
# ═══════════════════════════════════════════════════════════════════════════════
def chart_oom_defense(rows: list[dict]):
    if not rows:
        return

    oom_rows = [r for r in rows if (r.get("error_category") or "").startswith("Out_Of_Memory")]
    if not oom_rows:
        print("  ⚠️  OOM 데이터 없음. 스킵.")
        return

    total   = len(oom_rows)
    success = sum(1 for r in oom_rows if r["result_category"] == "SUCCESS")
    failure = total - success
    avg_ms  = sum(r["latency_sec"] * 1000 for r in oom_rows) / total

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("OOM(Out of Memory) 방어 성공률", fontsize=15, fontweight="bold",
                 color=TEXT, y=1.01)

    # ── 왼쪽: 도넛 ───────────────────────────────────────────────────
    ax = axes[0]
    wedges, _, autotexts = ax.pie(
        [success, failure],
        colors=[C_SUCCESS, C_FAILURE],
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops={"width": 0.55, "edgecolor": BG, "linewidth": 3},
        pctdistance=0.75,
    )
    for at in autotexts:
        at.set_fontsize(13); at.set_fontweight("bold"); at.set_color(BG)

    ax.text(0, 0.08, f"{success}/{total}", ha="center", fontsize=20,
            fontweight="bold", color=TEXT)
    ax.text(0, -0.22, "방어 성공", ha="center", fontsize=11, color=TEXT)

    patches = [
        mpatches.Patch(color=C_SUCCESS, label=f"방어 성공 ({success}건)"),
        mpatches.Patch(color=C_FAILURE, label=f"방어 실패 ({failure}건)"),
    ]
    ax.legend(handles=patches, loc="lower center", bbox_to_anchor=(0.5, -0.08),
              ncol=2, fontsize=10, framealpha=0.2)
    ax.set_title("OOM 방어 성공/실패", fontsize=13)

    # ── 오른쪽: KPI 텍스트 ───────────────────────────────────────────
    ax2 = axes[1]
    ax2.axis("off")
    kpis = [
        ("총 OOM 이벤트",     f"{total}건"),
        ("방어 성공률",        f"{success/total*100:.0f}%"),
        ("방어 실패(확대)",    f"{failure}건"),
        ("평균 복구 시간",     f"{avg_ms:.0f} ms"),
        ("선제 감지 임계값",   "VRAM ≥ 90%"),
        ("조치 방식",          "CLEAR_MEMORY"),
    ]
    for i, (label, value) in enumerate(kpis):
        y = 0.88 - i * 0.15
        ax2.text(0.05, y, label, transform=ax2.transAxes,
                 fontsize=11, color="#8899aa", va="center")
        ax2.text(0.6, y, value,  transform=ax2.transAxes,
                 fontsize=13, fontweight="bold", color=TEXT, va="center")
    ax2.set_title("OOM 방어 KPI 요약", fontsize=13)

    plt.tight_layout()
    out = CHART_DIR / "06_oom_defense.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"  ✓ {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  평가 그래프 생성 중...")
    print("=" * 55)

    rows = load_db()
    print(f"  DB 로드: {len(rows)}건\n")

    chart_l2_vs_l1()
    chart_mttr(rows)
    chart_success_by_category(rows)
    chart_result_distribution(rows)
    chart_latency_distribution(rows)
    chart_oom_defense(rows)

    print(f"\n  저장 경로: {CHART_DIR}")
    print("=" * 55)
