"""
audit_online_learning_entries.py
learn_from_feedback()으로 생성된 L1 캐시 엔트리(error_category="Learned_from_LLM")를
감사하고, provenance 태깅(source="online_learning")·카운터(success_count/failure_count)가
없는 구버전 엔트리에 소급 반영(backfill)한다.

배경 (2026-09-08): learn_from_feedback()이 이미 실제 운영 중이었지만 지금까지 source
태그도 성공/실패 카운터도 없이 만들어졌음 — 지금까지 실제로 몇 건이나 쌓였는지
이 스크립트로 처음 확인하고, record_learned_outcome()의 반복실패 자동제거 로직이
정상 작동하도록 카운터 초기값을 채운다.

이미 값이 있는 필드는 절대 덮어쓰지 않는다(setdefault) — 실제 누적된 성공/실패
이력을 리셋하면 반복실패 감지가 무의미해지므로, source/success_count/failure_count가
전부 없는(순수 구버전) 엔트리에만 backfill한다.

기본은 감사(리포트)만 하고 DB를 바꾸지 않는다 — --backfill을 명시해야 실제로 반영한다.

실행:
    cd ~/agent && sudo .venv/bin/python -m scripts.audit_online_learning_entries
    sudo .venv/bin/python -m scripts.audit_online_learning_entries --backfill
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

BASE_DIR         = Path(__file__).parent.parent
CHROMA_DIR       = str(BASE_DIR / "data" / "chroma_db")
COLLECTION       = "error_playbook_vectors"
LEARNED_CATEGORY = "Learned_from_LLM"
SOURCE           = "online_learning"


def run(backfill: bool) -> None:
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col  = client.get_collection(COLLECTION)
    data = col.get(include=["metadatas", "documents"])

    learned = [
        (data["ids"][i], data["metadatas"][i])
        for i in range(len(data["ids"]))
        if data["metadatas"][i].get("error_category") == LEARNED_CATEGORY
    ]
    tagged   = [e for e in learned if e[1].get("source") == SOURCE]
    untagged = [e for e in learned if e[1].get("source") != SOURCE]

    print("=" * 70)
    print("  온라인학습(learn_from_feedback) 엔트리 감사")
    print("=" * 70)
    print(f"  전체 ChromaDB 엔트리: {col.count()}개")
    print(f"  Learned_from_LLM 엔트리: {len(learned)}개")
    print(f"    - 이미 source 태깅됨: {len(tagged)}개")
    print(f"    - 태깅 없음(구버전): {len(untagged)}개")

    if not learned:
        print("\n온라인학습으로 생성된 엔트리가 아직 없음 — 감사할 대상 없음.")
        return
    if not untagged:
        print("\n백필 대상 없음, 전부 이미 태깅돼 있음.")
        return

    print("\n  태깅 없는 엔트리 목록:")
    for doc_id, meta in untagged:
        print(f"    - {doc_id[:28]:<30} command={meta.get('command', '')[:50]!r}")

    if not backfill:
        print("\n[감사만] --backfill 없이 실행됨, DB 변경 없음")
        return

    for doc_id, meta in untagged:
        meta = dict(meta)
        meta.setdefault("source", SOURCE)
        meta.setdefault("success_count", 1)  # 이미 성공 경로로만 생성되므로 최소 1
        meta.setdefault("failure_count", 0)
        col.update(ids=[doc_id], metadatas=[meta])

    print(f"\n완료: {len(untagged)}개 엔트리에 provenance 태깅 백필")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill", action="store_true",
        help="태깅 없는 엔트리에 실제로 반영(기본은 감사 리포트만 출력, DB 변경 없음)",
    )
    args = parser.parse_args()
    run(backfill=args.backfill)
