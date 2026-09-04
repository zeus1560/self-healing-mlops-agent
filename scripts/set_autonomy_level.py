#!/usr/bin/env python3
"""
카테고리별 Progressive Autonomy 레벨을 수동으로 확인·변경하는 CLI.

승급/강등은 이 스크립트를 통해서만 일어난다 — 코드 어디에도 자동 승급 로직은 없다
(2026-09-03 /grill-me 세션 결정: 최종 승급은 사람이 로그를 보고 수동 확인).

Usage:
    python -m scripts.set_autonomy_level --list
    python -m scripts.set_autonomy_level Process_Crash approve_then_execute --note "9/4 첫 배포 기본값"
    python -m scripts.set_autonomy_level Process_Crash auto --note "Shadow gate 통과, 9/20 확인"
    python -m scripts.set_autonomy_level Process_Crash --shadow auto
"""
import argparse
import getpass

from src import autonomy_store
from src.schemas import AutonomyLevel

_LEVEL_CHOICES = [lvl.value for lvl in AutonomyLevel]


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("등록된 카테고리 없음 — 전부 기본값"
              f" ({autonomy_store.DEFAULT_AUTONOMY_LEVEL.value}) 사용 중.")
        return
    header = f"{'카테고리':<24}{'레벨':<22}{'Shadow 목표':<22}{'변경일시':<26}{'변경자'}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['category']:<24}{r['level']:<22}"
            f"{(r['shadow_target_level'] or '-'):<22}"
            f"{r['updated_at']:<26}{r['updated_by'] or '-'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="전체 카테고리 현재 레벨 표시")
    parser.add_argument("category", nargs="?", help="변경할 ErrorCategory 이름")
    parser.add_argument("level", nargs="?", choices=_LEVEL_CHOICES, help="설정할 레벨")
    parser.add_argument("--shadow", choices=_LEVEL_CHOICES, default=None,
                         help="레벨을 바로 바꾸지 않고 해당 목표 레벨로 승급 검토(Shadow) 시작")
    parser.add_argument("--note", default="", help="변경 사유 메모")
    args = parser.parse_args()

    autonomy_store.init_table()

    if args.list:
        _print_table(autonomy_store.list_all())
        return

    if not args.category:
        if args.shadow or args.level:
            parser.error("--shadow/level을 지정하려면 category도 함께 지정해야 합니다.")
        _print_table(autonomy_store.list_all())
        return

    updated_by = getpass.getuser()

    if args.shadow:
        autonomy_store.start_shadow(args.category, AutonomyLevel(args.shadow), updated_by)
        print(
            f"'{args.category}' → Shadow 승급 검토 시작 "
            f"(목표: {args.shadow}). experiments/run_shadow_gate_report.py로 진행 상황 확인."
        )
        return

    if not args.level:
        parser.error("category만 지정한 경우 level 또는 --shadow가 필요합니다.")

    autonomy_store.set_level(args.category, AutonomyLevel(args.level), updated_by, args.note)
    print(f"'{args.category}' → {args.level} 로 변경 완료 (변경자: {updated_by}).")


if __name__ == "__main__":
    main()
