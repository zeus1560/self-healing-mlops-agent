"""
etl_backup.json → data/train_set.json + data/test_set.json
error_category 기준 stratified 80/20 split.
"""
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

BACKUP_PATH = Path("data/etl_backup.json")
TRAIN_PATH  = Path("data/train_set.json")
TEST_PATH   = Path("data/test_set.json")
TEST_RATIO  = 0.2
SEED        = 42


def stratified_split(data: list[dict], test_ratio: float, seed: int):
    random.seed(seed)
    by_category: dict[str, list] = defaultdict(list)
    for item in data:
        by_category[item["error_category"]].append(item)

    train, test = [], []
    for category, items in sorted(by_category.items()):
        random.shuffle(items)
        n_test = int(len(items) * test_ratio)

        # 카테고리 전체가 test로 빠지지 않도록 train 최소 1건 보장.
        # n_test가 0이면 해당 카테고리는 test 미포함으로 진행(경고만).
        if n_test >= len(items):
            n_test = len(items) - 1
            print(
                f"  [WARN] {category}: 항목 {len(items)}개로 20% 분리 불가 "
                f"→ train 1건 강제 확보, test {n_test}건",
                file=sys.stderr,
            )

        test.extend(items[:n_test])
        train.extend(items[n_test:])
        print(
            f"  {category}: 전체 {len(items)} "
            f"→ train {len(items) - n_test}, test {n_test}"
        )

    return train, test


if __name__ == "__main__":
    if not BACKUP_PATH.exists():
        print(f"[ERROR] 백업 파일 없음: {BACKUP_PATH}", file=sys.stderr)
        sys.exit(1)

    try:
        raw = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ERROR] 백업 파일 읽기 실패: {e}", file=sys.stderr)
        sys.exit(1)

    items = raw.get("data", [])
    if not items:
        print("[ERROR] 백업 데이터가 비어 있습니다.", file=sys.stderr)
        sys.exit(1)

    print(f"전체 데이터: {len(items)}개\n카테고리별 분리:")
    train, test = stratified_split(items, TEST_RATIO, SEED)

    try:
        TRAIN_PATH.write_text(
            json.dumps({"total": len(train), "data": train}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        TEST_PATH.write_text(
            json.dumps({"total": len(test), "data": test}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[ERROR] 결과 파일 저장 실패: {e}", file=sys.stderr)
        sys.exit(1)

    total = len(items)
    print(f"\n결과: train {len(train)}개 → {TRAIN_PATH}")
    print(f"결과: test  {len(test)}개  → {TEST_PATH}")
    print(f"비율: train {len(train)/total*100:.1f}% / test {len(test)/total*100:.1f}%")
