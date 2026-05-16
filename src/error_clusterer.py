"""
에러 패턴 자동 클러스터링.

ChromaDB에 쌓인 벡터를 주기적으로 KMeans 클러스터링해서
새로운 에러 카테고리 후보를 발견한다.

- sklearn 미설치 시 graceful no-op
- MaintenanceRunner.run_if_due() 주기(1일)에 맞춰 log_watcher에서 호출
- 이전 실행 결과와 비교해 신규 카테고리 발견 시 new_patterns 반환
"""
import json
import logging
import os
import traceback
from typing import Optional

_SKLEARN_AVAILABLE = False
try:
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    import numpy as np
    _SKLEARN_AVAILABLE = True
except ImportError:
    pass

_N_CLUSTERS  = int(os.getenv("CLUSTER_N_CLUSTERS",  "8"))   # 탐색할 클러스터 수
_MIN_VECTORS = int(os.getenv("CLUSTER_MIN_VECTORS", "20"))  # 클러스터링에 필요한 최소 벡터 수
_STATE_PATH  = os.getenv("CLUSTER_STATE_PATH", "./data/cluster_state.json")


class ErrorClusterer:
    def __init__(self, chroma_collection=None):
        """
        chroma_collection: RAGEngine.collection (외부에서 주입).
        None이면 실행 시 Singleton 클라이언트로 직접 가져온다.
        """
        self._collection = chroma_collection

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        from src.llm_engine import _get_chroma_client
        client = _get_chroma_client()
        return client.get_or_create_collection("error_playbook_vectors")

    def _load_prev_categories(self) -> set[str]:
        try:
            with open(_STATE_PATH) as f:
                return set(json.load(f).get("categories", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_categories(self, categories: set[str]) -> None:
        try:
            os.makedirs(os.path.dirname(_STATE_PATH) or ".", exist_ok=True)
            with open(_STATE_PATH, "w") as f:
                json.dump({"categories": sorted(categories)}, f)
        except Exception:
            logging.warning(f"[ErrorClusterer] 상태 저장 실패:\n{traceback.format_exc()}")

    def run(self) -> Optional[dict]:
        """
        클러스터링 실행.
        반환: {"n_vectors": int, "n_clusters": int, "silhouette": float,
               "cluster_labels": list[str], "new_patterns": list[str]}
        sklearn 미설치 or 벡터 부족 시 None 반환.
        """
        if not _SKLEARN_AVAILABLE:
            logging.warning("[ErrorClusterer] scikit-learn 미설치 — 클러스터링 생략.")
            return None

        try:
            return self._run_inner()
        except Exception:
            logging.error(f"[ErrorClusterer] 실패:\n{traceback.format_exc()}")
            return None

    def _run_inner(self) -> Optional[dict]:
        col     = self._get_collection()
        count   = col.count()

        if count < _MIN_VECTORS:
            logging.info(f"[ErrorClusterer] 벡터 {count}개 < {_MIN_VECTORS} — 생략.")
            return None

        # ChromaDB의 모든 임베딩을 가져온다 (include=["embeddings", "metadatas"])
        data       = col.get(include=["embeddings", "metadatas"])
        embeddings = np.array(data["embeddings"])
        metadatas  = data["metadatas"]

        n_clusters = min(_N_CLUSTERS, count)
        km = KMeans(n_clusters=n_clusters, random_state=42, n_init="auto")
        labels = km.fit_predict(embeddings)

        sil = float(silhouette_score(embeddings, labels)) if n_clusters > 1 else 0.0

        # 클러스터별 대표 error_category (다수결)
        from collections import Counter
        cluster_labels = []
        current_categories: set[str] = set()
        for c in range(n_clusters):
            idxs = [i for i, l in enumerate(labels) if l == c]
            cats = [metadatas[i].get("error_category", "Unknown") for i in idxs]
            top  = Counter(cats).most_common(1)[0][0]
            cluster_labels.append(f"Cluster{c}:{top}({len(idxs)})")
            current_categories.add(top)

        # 이전 실행 대비 신규 카테고리 감지
        prev_categories = self._load_prev_categories()
        new_patterns    = sorted(current_categories - prev_categories)
        self._save_categories(current_categories)

        logging.info(
            f"[ErrorClusterer] 완료 | 벡터:{count} 클러스터:{n_clusters} "
            f"실루엣:{sil:.3f}\n  " + "\n  ".join(cluster_labels)
        )
        if new_patterns:
            logging.warning(f"[ErrorClusterer] 신규 에러 패턴 감지: {new_patterns}")

        return {
            "n_vectors":      count,
            "n_clusters":     n_clusters,
            "silhouette":     sil,
            "cluster_labels": cluster_labels,
            "new_patterns":   new_patterns,
        }
