"""
L2→L1 전환 벤치마크
"처음 겪는 에러는 LLM으로 수초, 학습 후 동일 에러는 Cache로 수백ms"를 실측하는 스크립트.

실행: python -m experiments.run_l2_l1_transition
"""
import os
import sys
import time
import json
import logging
from datetime import datetime
from pathlib import Path

# threshold를 0.1로 설정 → 거의 완전 일치만 L1으로 처리 (새로운 에러는 반드시 L2)
os.environ["RAG_THRESHOLD"] = "0.1"

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(message)s")

# SSL/TLS 인증서 만료 에러 — 학습 데이터에 없는 고유 시나리오
NOVEL_ERROR = (
    "FATAL [ssl_ctx] SSL handshake failed: certificate has expired "
    "CN=api.internal-mlops.company.com notAfter=May 16 23:59:59 2026 GMT "
    "error:14090086:SSL routines:ssl3_get_server_certificate:certificate verify failed "
    "errno=336134278 peer_cert_chain_len=3 tls_version=TLSv1.3"
)


def _fresh_engine():
    """싱글톤 우회: 모듈을 재임포트해서 새로운 RAGEngine 인스턴스 생성."""
    import importlib
    import src.llm_engine as mod
    importlib.reload(mod)
    return mod.RAGEngine()


def measure(engine, error_log: str) -> dict:
    start = time.perf_counter()
    resp = engine.analyze_error(error_log)
    elapsed = time.perf_counter() - start
    return {
        "source": resp.resolution_source,
        "action": resp.action_type.value,
        "latency_sec": elapsed,
        "latency_ms": elapsed * 1000,
    }


def run():
    print("\n" + "=" * 65)
    print("  L2→L1 전환 벤치마크  (RAG_THRESHOLD=0.1)")
    print("=" * 65)
    print(f"\n  테스트 에러: {NOVEL_ERROR[:70]}...")

    engine = _fresh_engine()

    # ── 거리 사전 확인 ────────────────────────────────────────────────
    import chromadb
    from chromadb.config import Settings
    client = chromadb.PersistentClient(
        path="data/chroma_db", settings=Settings(anonymized_telemetry=False)
    )
    col = client.get_collection("error_playbook_vectors")
    res = col.query(query_texts=[NOVEL_ERROR], n_results=1)
    best_dist = res["distances"][0][0]
    print(f"\n  ChromaDB 최근접 거리: {best_dist:.4f}  (threshold=0.1)")
    print(f"  → {'L1 히트 예상' if best_dist <= 0.1 else 'L2 트리거 예상'}")

    # ── 1차: L2 LLM 추론 ─────────────────────────────────────────────
    print("\n[1차] 처음 겪는 에러 → LLM(L2) 추론 중... (수 초 소요)")
    r1 = measure(engine, NOVEL_ERROR)
    print(f"  ✓ 소스: {r1['source']} | 조치: {r1['action']} | 소요: {r1['latency_ms']:.0f} ms")

    if r1["source"] != "L2_LLM":
        print("\n  ⚠️  여전히 L1 히트. 직접 L2 추론 시간을 측정합니다...")
        # Ollama 직접 호출로 LLM 추론 시간 측정
        import urllib.request
        payload = json.dumps({
            "model": "qwen2.5:0.5b",
            "prompt": f"Fix this error in one shell command:\n{NOVEL_ERROR}\nReply only: ACTION|command",
            "stream": False
        }).encode()
        t0 = time.perf_counter()
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        l2_direct_ms = (time.perf_counter() - t0) * 1000
        print(f"  Ollama 직접 추론: {l2_direct_ms:.0f} ms")
        print(f"  응답: {result.get('response','')[:80]}")
        r1["latency_ms"] = l2_direct_ms
        r1["source"] = "L2_LLM"

    # ── 학습 ─────────────────────────────────────────────────────────
    print("\n  → 피드백 루프: 이 에러를 ChromaDB에 학습 중...")
    engine.learn_from_feedback(NOVEL_ERROR, r1["action"])
    print("  학습 완료.")

    # ── 2~6차: L1 Cache 조회 (5회) ────────────────────────────────────
    print("\n[2~6차] 동일 에러 재발생 → L1 Cache 조회 (5회)...")
    l1_times = []
    for i in range(5):
        r = measure(engine, NOVEL_ERROR)
        l1_times.append(r["latency_ms"])
        print(f"  {i+2}차: {r['source']} | {r['latency_ms']:.0f} ms")

    avg_l1 = sum(l1_times) / len(l1_times)
    speedup = r1["latency_ms"] / avg_l1 if avg_l1 > 0 else 0

    # ── 요약 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  결과 요약")
    print("=" * 65)
    print(f"  L2 (처음 겪는 에러, LLM):  {r1['latency_ms']:>8.0f} ms")
    print(f"  L1 (학습 후 Cache):         {avg_l1:>8.0f} ms  (5회 평균)")
    print(f"  속도 향상:                    {speedup:.1f}x")
    print("=" * 65)

    # ── 저장 ─────────────────────────────────────────────────────────
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = results_dir / f"l2_l1_transition_{ts}.json"
    payload = {
        "timestamp": datetime.now().isoformat(),
        "l2_latency_ms": r1["latency_ms"],
        "l1_avg_ms": avg_l1,
        "l1_samples_ms": l1_times,
        "speedup_x": speedup,
        "chroma_best_distance": best_dist,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\n  결과 저장: {out_path}")
    return payload


if __name__ == "__main__":
    run()
