#!/usr/bin/env bash
# Self-Healing MLOps Agent — 원클릭 설치 스크립트
# 사용법: bash install.sh
set -euo pipefail

PYTHON=${PYTHON:-python3}
VENV_DIR=".venv"

echo "============================================================"
echo "  Self-Healing MLOps Agent  —  설치 시작"
echo "============================================================"

# ── 1. Python 버전 확인 ──────────────────────────────────────────────
echo "[1/7] Python 버전 확인..."
$PYTHON -c "import sys; assert sys.version_info >= (3,10), f'Python 3.10+ 필요 (현재: {sys.version})'"
echo "  OK: $($PYTHON --version)"

# ── 2. 가상환경 생성 ─────────────────────────────────────────────────
echo "[2/7] 가상환경 생성: $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
    echo "  생성 완료."
else
    echo "  기존 가상환경 재사용."
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 3. pip 업그레이드 + 패키지 설치 ─────────────────────────────────
echo "[3/7] 패키지 설치 (requirements.txt)..."
pip install --upgrade pip --quiet
if [ -f requirements.txt ]; then
    pip install -r requirements.txt --quiet
    echo "  설치 완료."
else
    echo "  [WARN] requirements.txt 없음 — 개별 설치를 진행합니다."
    pip install --quiet \
        chromadb watchdog requests python-dotenv \
        streamlit altair pandas matplotlib
fi

# ── 4. .env 설정 ─────────────────────────────────────────────────────
echo "[4/7] 환경변수 파일 확인..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  .env.example → .env 복사 완료."
    echo "  !! .env 파일을 열어 GITHUB_TOKEN 등 실제 값을 채워주세요."
else
    echo "  .env 이미 존재 — 건너뜀."
fi

# ── 5. data 디렉터리 생성 ────────────────────────────────────────────
echo "[5/7] data/ 디렉터리 준비..."
mkdir -p data/chroma_db experiments/results
echo "  OK"

# ── 6. Ollama 설치 안내 ──────────────────────────────────────────────
echo "[6/7] Ollama 확인..."
if command -v ollama &>/dev/null; then
    echo "  Ollama 설치됨: $(ollama --version 2>/dev/null || echo '버전 확인 불가')"
    echo "  모델 풀: ollama pull qwen2.5:0.5b"
else
    echo "  [INFO] Ollama 미설치. L2 LLM 추론을 사용하려면 아래 명령어로 설치하세요:"
    echo "    curl -fsSL https://ollama.com/install.sh | sh"
    echo "    ollama pull qwen2.5:0.5b"
fi

# ── 7. 초기 ETL + Vector DB 구축 안내 ──────────────────────────────
echo "[7/7] 초기 데이터 구축 방법 안내..."
cat <<'GUIDE'

  다음 명령어로 에러 플레이북 데이터를 구축하세요:

  # (선택) GitHub 이슈 크롤링
  python -m src.etl_github_crawler

  # JSON → SQLite 적재
  python -m src.etl_ingest

  # train/test 분할 (80/20)
  python scripts/split_dataset.py

  # ChromaDB 벡터 인덱싱
  python -m src.etl_vector_sync

  # 에이전트 실행
  python main.py

  # 대시보드
  streamlit run dashboard/app.py

  # 실험 전체 실행
  python experiments/run_all.py --skip 13   # Ollama 없을 경우 13번 스킵

GUIDE

echo "============================================================"
echo "  설치 완료!"
echo "  가상환경 활성화: source $VENV_DIR/bin/activate"
echo "============================================================"
