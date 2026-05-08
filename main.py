"""
main.py — Intel Arc GPU 실시간 모니터 데몬

5초마다 GPU 상태를 폴링하여 터미널에 출력합니다.
종료: Ctrl+C
"""
import re
import sys
import time
import datetime

from src.monitor.vram_profiler import get_intel_gpu_stats

# ── 설정 ──────────────────────────────────────────────
POLL_INTERVAL = 5     # 폴링 간격 (초)
BAR_WIDTH     = 22    # 진행 막대 길이 (visible chars)
BOX_WIDTH     = 58    # 박스 내부 너비 (visible chars, ANSI 코드 제외)

# ── ANSI 색상 코드 ──────────────────────────────────────
RST  = "\033[0m"
BOLD = "\033[1m"
DIM  = "\033[2m"
CYN  = "\033[96m"    # 하늘색 — 박스 테두리
GRN  = "\033[92m"    # 녹색   — 낮은 사용률 (< 50 %)
YLW  = "\033[93m"    # 노란색 — 중간 사용률 (50–80 %)
RED  = "\033[91m"    # 빨간색 — 높은 사용률 (> 80 %)
WHT  = "\033[97m"    # 밝은 흰색

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


# ── 박스 출력 헬퍼 ──────────────────────────────────────

def _vlen(s: str) -> int:
    """ANSI 이스케이프 코드를 제외한 실제 출력 문자 수."""
    return len(_ANSI_RE.sub("", s))


def _box_line(text: str = "") -> None:
    """내용을 ║ text ░░ ║ 형태로 출력. ANSI 코드를 제외하고 패딩을 계산한다."""
    pad = BOX_WIDTH - _vlen(text)
    print(f"{CYN}║{RST} {text}{' ' * max(pad, 0)} {CYN}║{RST}")


def _separator(pos: str = "mid") -> None:
    """박스 구분선. pos: 'top' | 'mid' | 'bot'"""
    chars = {
        "top": ("╔", "═", "╗"),
        "mid": ("╠", "═", "╣"),
        "bot": ("╚", "═", "╝"),
    }[pos]
    l, f, r = chars
    print(f"{CYN}{l}{f * (BOX_WIDTH + 2)}{r}{RST}")


# ── 값 포매터 ───────────────────────────────────────────

def _color(ratio: float) -> str:
    return RED if ratio > 0.8 else (YLW if ratio > 0.5 else GRN)


def _bar(value, max_val=100.0) -> str:
    """ANSI 컬러 진행 막대를 반환한다."""
    if value is None or max_val is None or max_val == 0:
        return f"{DIM}{'─' * BAR_WIDTH}{RST}"
    ratio = min(value / max_val, 1.0)
    filled = round(ratio * BAR_WIDTH)
    col = _color(ratio)
    return f"{col}{'█' * filled}{'░' * (BAR_WIDTH - filled)}{RST}"


def _pct(value) -> str:
    if value is None:
        return f"{DIM}  N/A  {RST}"
    col = _color(value / 100.0)
    return f"{BOLD}{col}{value:5.1f}%{RST}"


def _mb(used, total) -> str:
    if used is None:
        return f"{DIM}N/A{RST}"
    if total:
        return f"{WHT}{used:,.0f}{RST}{DIM} /{RST} {WHT}{total:,.0f} MiB{RST}"
    return f"{WHT}{used:,.0f} MiB{RST}"


# ── 렌더 ────────────────────────────────────────────────

def render(stats: dict) -> None:
    """stats dict를 받아 터미널 박스 UI로 출력한다."""
    now       = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    gpu_load  = stats.get("gpu_load_percent")
    vram_used = stats.get("vram_used_mb")
    vram_tot  = stats.get("vram_total_mb")
    source    = stats.get("source", "unavailable")
    error     = stats.get("error")

    vram_pct = (vram_used / vram_tot * 100) if (vram_used and vram_tot) else None

    # 화면 클리어 (홈 → 클리어)
    print("\033[H\033[J", end="")

    # 헤더
    _separator("top")
    title = f"{BOLD}{WHT}  Intel Arc GPU  │  Real-time Monitor{RST}"
    _box_line(title)
    _separator("mid")

    # 기본 정보
    _box_line(f"{DIM}시각{RST}   {WHT}{now}{RST}")
    _box_line(f"{DIM}소스{RST}   {CYN}{source}{RST}")
    _separator("mid")

    # GPU Load
    gpu_row = (
        f"{DIM}GPU Load{RST}  "
        f"{_bar(gpu_load)}  "
        f"{_pct(gpu_load)}"
    )
    _box_line(gpu_row)

    # VRAM
    vram_row = (
        f"{DIM}VRAM    {RST}  "
        f"{_bar(vram_used, vram_tot or 100)}  "
        f"{_mb(vram_used, vram_tot)}"
    )
    _box_line(vram_row)

    _separator("bot")

    # 하단 상태 메시지
    if error:
        print(f"\n  {YLW}⚠  {error}{RST}")
    else:
        print(f"\n  {DIM}갱신 주기: {POLL_INTERVAL}s   │   종료: Ctrl+C{RST}")


# ── 진입점 ──────────────────────────────────────────────

def main() -> None:
    print(f"{CYN}Intel Arc GPU 모니터 초기화 중…{RST}", flush=True)
    time.sleep(0.2)

    try:
        while True:
            stats = get_intel_gpu_stats()
            render(stats)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n\n  {DIM}모니터가 종료되었습니다.{RST}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
