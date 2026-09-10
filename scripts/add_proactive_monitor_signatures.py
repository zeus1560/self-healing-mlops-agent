"""
add_proactive_monitor_signatures.py
ProactiveMonitor(src/proactive_monitor.py)가 실제로 만드는 합성 경고 문구를
ChromaDB에 추가한다.

배경 (2026-09-10): VM 실측 중 디스크 92% 경보가 ProactiveMonitor를 통해 파이프라인을
선제 발동시켰는데, L1이 미스(카오스 인젝터 문구와 어휘가 달라 거리 초과)돼 L2/Groq로
넘어갔고, Groq가 즉석에서 만든 `find ... && find ... -delete` 복합 명령어가 보안
화이트리스트(토큰 수 상한 + 메타문자 차단 + `find` 자체가 화이트리스트에 없음)에
3중으로 막혀 계속 실패 → Circuit Breaker 실패 누적. 이건 화이트리스트를 완화해서
고칠 문제가 아니다(체이닝 차단은 핵심 보안 경계) — 이미 검증된 안전한 대응
(add_chaos_injector_signatures.py의 Disk_Full→`journalctl --vacuum-size 1G`,
Out_Of_Memory→`clear_memory`)이 있는데 ProactiveMonitor의 문구만 그 학습 데이터와
어휘가 달라 L1 히트를 못 하고 있었을 뿐 — add_chaos_injector_signatures.py와 같은
train-serving skew 패턴(2026-09-07에도 한 번 겪음)이라 같은 방식으로 고친다.

CPU(cpu 트리거)는 ErrorCategory 자체에 매핑이 없는 기존 구조적 공백(향후 별도
작업)이라 포함하지 않는다. VRAM은 Intel Arc GPU 전용(GCP VM엔 GPU 없음)이라 지금
배포 대상과 무관해 제외한다.

실행:
    cd ~/agent && sudo .venv/bin/python -m scripts.add_proactive_monitor_signatures
    sudo .venv/bin/python -m scripts.add_proactive_monitor_signatures --dry-run
    sudo .venv/bin/python -m scripts.add_proactive_monitor_signatures --remove
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

BASE_DIR   = Path(__file__).parent.parent
CHROMA_DIR = str(BASE_DIR / "data" / "chroma_db")
COLLECTION = "error_playbook_vectors"
ID_PREFIX  = "proactive_sig_v1_"
SOURCE     = "proactive_monitor_signature"

# src/proactive_monitor.py의 _check_disk()/_check_memory()가 f-string으로 만드는
# 것과 동일한 형태 — 퍼센트/용량 숫자는 매번 달라지지만(대표값 사용), 기존 chaos_sig
# 관례와 동일하게 임베딩은 숫자가 아니라 문장 형태로 매칭되므로 문제 없다.
_CLEAN_LINES: dict[str, list[str]] = {
    "Disk_Full": [
        "CRITICAL Disk usage 91.1% — no space left on device risk (free: 2GB)",
    ],
    "Out_Of_Memory": [
        "CRITICAL Memory usage 87.3% — OOM risk detected proactively (available: 512MB)",
    ],
}

# 이미 검증된 안전한 대응(add_chaos_injector_signatures.py의 ACTION_MAP과 동일 —
# 같은 카테고리는 반드시 같은 대응으로 맞춘다, 학습 데이터 내부 모순 방지).
ACTION_MAP: dict[str, tuple[str, str, str]] = {
    "Disk_Full":     ("execute_rule_command", "", "journalctl --vacuum-size 1G"),
    "Out_Of_Memory": ("clear_memory",         "", ""),
}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _build_entries() -> tuple[list[str], list[str], list[dict]]:
    ids, docs, metas = [], [], []
    for cat, (action, target, command) in ACTION_MAP.items():
        for text in _CLEAN_LINES.get(cat, []):
            uid = ID_PREFIX + _md5(text)
            ids.append(uid)
            docs.append(text)
            metas.append({
                "error_category": cat,
                "action_type":    action,
                "target_process": target,
                "command":        command,
                "source":         SOURCE,
            })
    return ids, docs, metas


def run(dry_run: bool = False) -> None:
    ids, docs, metas = _build_entries()

    print("=" * 70)
    print("  ProactiveMonitor 합성 경고 문구 → ChromaDB 추가")
    print("=" * 70)
    for cat in ACTION_MAP:
        n = sum(1 for m in metas if m["error_category"] == cat)
        print(f"  {cat:<22} {n}개")
    print(f"  {'합계':<22} {len(ids)}개")

    if dry_run:
        print("\n[--dry-run] ChromaDB 적재 건너뜀")
        return

    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col    = client.get_collection(COLLECTION)
    before = col.count()
    col.upsert(ids=ids, documents=docs, metadatas=metas)
    after = col.count()
    print(f"\n완료: {len(ids)}개 upsert | ChromaDB {before} → {after}개")

    print("\n" + "=" * 70)
    print("  L1 HIT 검증 (RAG_THRESHOLD=0.6, 실제 운영과 동일 임계값)")
    print("=" * 70)
    all_ok = True
    for cat in ACTION_MAP:
        probe = _CLEAN_LINES[cat][0]
        r = col.query(query_texts=[probe], n_results=1)
        d = r["distances"][0][0]
        m = r["metadatas"][0][0]
        hit = d <= 0.6
        if not hit:
            all_ok = False
        sym = "✅" if hit else "❌"
        print(f"  {sym} {cat:<22} d={d:.4f}  matched={m.get('error_category')}")
    print(f"\n{'전체 통과 ✅' if all_ok else '일부 실패 ❌ — 임계값 자체를 재검토할 필요 있음'}")


def remove_entries() -> None:
    client = chromadb.PersistentClient(
        path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False)
    )
    col  = client.get_collection(COLLECTION)
    data = col.get(include=["metadatas"])
    rm   = [data["ids"][i] for i, m in enumerate(data["metadatas"]) if m.get("source") == SOURCE]
    if rm:
        col.delete(ids=rm)
    print(f"삭제 완료: {len(rm)}개 | 현재 {col.count()}개")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="추가할 내용만 출력, DB 적재 안 함")
    parser.add_argument("--remove",  action="store_true", help="이 스크립트가 추가한 항목만 삭제")
    args = parser.parse_args()

    if args.remove:
        remove_entries()
    else:
        run(dry_run=args.dry_run)
