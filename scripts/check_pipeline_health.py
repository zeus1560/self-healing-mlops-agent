"""
scripts/check_pipeline_health.py

파이프라인 자기 감시 — chaos_cron.sh는 계속 정상 주입하는데 실제 파이프라인
(log_watcher → L1/L2 → AgentObserver)이 조용히 기록/처리에 실패하는 상황을 잡는다.

배경(2026-09-11~14 실제 사고): observability.py의 컬럼명 안전성 정규식이
`l1_evidence`(숫자 포함)를 "비안전 컬럼"으로 오판해 스키마 마이그레이션을 계속
건너뛰었다. 그 결과 모든 log_event() 호출이 "no column named l1_evidence"로
INSERT에 실패했지만 예외가 내부에서 조용히 삼켜져서(로그만 남고 프로세스는
안 죽음) systemd 서비스는 계속 active로 보였다. chaos_cron.sh는 6시간마다 정상적으로
fault를 주입했지만 agent_metrics.db에는 3일간 단 한 건도 기록되지 않았고,
사람이 직접 sqlite3로 들여다보기 전까지 아무도 몰랐다.

이 스크립트는 experiments.run_fp_fn_analysis.analyze()가 이미 하는 매칭 로직
(chaos_injector.log의 주입 이벤트 vs agent_metrics.db의 실제 기록)을 그대로
재사용해, 최근 lookback_hours 안에서 "주입은 있었는데 파이프라인 반응 흔적이
전혀 없는" 비율이 비정상적으로 높으면 기존 Telegram/Slack 알림 경로로 경보를
보낸다. 새 데이터 소스나 새 판정 로직을 만들지 않는다 — 이미 있는 두 산출물만
다른 각도(개별 카테고리 recall이 아니라 "파이프라인이 아예 반응했는가")로 본다.

실행: sudo .venv/bin/python -m scripts.check_pipeline_health
      (다른 분석 스크립트와 동일하게 root 소유 data/*.db 읽기 위해 sudo 필요)
크론 등록 권장: 하루 1회(예: `0 9 * * *`) — chaos_cron.sh의 6시간 주기보다 넉넉하게
잡아야 "주입 몇 건 없어서 우연히 다 놓침" 같은 정상 변동을 오탐하지 않는다.
"""
import argparse
import logging
import traceback
from datetime import datetime, timedelta, timezone

from experiments.run_fp_fn_analysis import analyze

# 이 건수 미만이면 비율이 튀어도(예: 1건 중 1건 미탐 = 100%) 판단하지 않는다 —
# 표본이 너무 작을 때의 오탐 방지.
MIN_EVENTS_TO_JUDGE = 2
# 이 비율 이상 "완전 미탐지"면 개별 분류 실수가 아니라 파이프라인 자체가
# 죽어있다고 보고 경보한다(정상 상태의 미탐은 카테고리 몇 개에 국한되지, 최근
# 주입 전체가 통째로 안 잡히는 일은 없다).
MISS_RATIO_ALERT_THRESHOLD = 0.8


def check(lookback_hours: float = 24.0) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    summary = analyze(since=since)
    total = summary["chaos_injector_log_events"]
    missed = summary["missed_entirely"]
    miss_ratio = missed / total if total else 0.0
    alert = total >= MIN_EVENTS_TO_JUDGE and miss_ratio >= MISS_RATIO_ALERT_THRESHOLD
    return {
        **summary,
        "lookback_hours": lookback_hours,
        "miss_ratio": round(miss_ratio, 4),
        "alert": alert,
    }


def _notify(message: str) -> None:
    """기존 AgentObserver 경보 경로(observability.py)와 동일한 Telegram→Slack 폴백 패턴."""
    try:
        from src.telegram_bot import get_chatops_client
        tg = get_chatops_client()
    except Exception:
        # 2026-09-15 code-review 발견: 이 예외를 그냥 삼키면 claude.md의 "모든 예외는
        # traceback.format_exc()로 기록해야 한다" 원칙 위반 — Telegram 미설정(정상)과
        # 진짜 임포트 실패를 구분할 방법이 없어진다. Slack 폴백은 그대로 시도하되 기록은 남긴다.
        logging.error(f"[HealthCheck] Telegram 클라이언트 로드 실패:\n{traceback.format_exc()}")
        tg = None

    if tg:
        try:
            tg.send_notification("⚠️ 파이프라인 헬스체크 경보", message)
            return
        except Exception:
            logging.error("[HealthCheck] Telegram 알림 전송 실패", exc_info=True)

    try:
        from src.slack_bot import SlackChatOps
        SlackChatOps().send_notification("⚠️ 파이프라인 헬스체크 경보", message)
    except Exception:
        logging.error("[HealthCheck] Slack 알림 전송 실패", exc_info=True)


def main(lookback_hours: float = 24.0, notify: bool = True) -> dict:
    # 2026-09-15 code-review 발견: check()가 raise하면(예: DB 스키마/권한 문제) 이
    # 스크립트 자체가 크론에서 조용히 죽어버려서, "파이프라인이 조용히 고장나는 걸
    # 잡는 안전망"이 스스로 같은 방식으로 고장나는 역설이 생긴다. 안전망 자체의
    # 실패도 경보 대상으로 취급한다 — 원래 알려야 했던 지표는 모르지만, "헬스체크가
    # 돌지 않았다"는 사실 자체는 사람에게 반드시 전달되어야 한다.
    try:
        result = check(lookback_hours)
    except Exception:
        tb = traceback.format_exc()
        message = f"파이프라인 헬스체크 스크립트 자체가 실행 중 예외로 실패했습니다:\n{tb[-500:]}"
        print(f"[HEALTHCHECK FAILURE] {message}")
        if notify:
            _notify(message)
        raise

    if result["alert"]:
        message = (
            f"최근 {lookback_hours:.0f}시간 동안 카오스 주입 {result['chaos_injector_log_events']}건 중 "
            f"{result['missed_entirely']}건({result['miss_ratio'] * 100:.0f}%)이 agent_metrics.db에 "
            f"전혀 기록되지 않았습니다. 서비스는 살아있어도 조용히 기록/처리에 실패하고 있을 "
            f"가능성이 있습니다(2026-09-11~14 l1_evidence 스키마 마이그레이션 버그와 동일 패턴). "
            f"journalctl -u self-healing-agent 로 최근 에러를 바로 확인하세요."
        )
        print(f"[ALERT] {message}")
        if notify:
            _notify(message)
    else:
        print(
            f"[OK] 최근 {lookback_hours:.0f}시간: 주입 {result['chaos_injector_log_events']}건, "
            f"미탐 {result['missed_entirely']}건({result['miss_ratio'] * 100:.0f}%) — 정상 범위."
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument(
        "--no-notify", action="store_true",
        help="경보 조건이어도 실제 알림은 보내지 않음(테스트/드라이런용)",
    )
    args = parser.parse_args()
    main(lookback_hours=args.lookback_hours, notify=not args.no_notify)
