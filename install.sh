#!/usr/bin/env bash
# Self-Healing MLOps Agent — 원클릭 설치 스크립트
#
# 새 서버 한 대를 git clone 이후 상태에서 `make start`(또는 `systemctl start
# self-healing-agent`)만 누르면 되는 상태까지 끌어올린다:
#   venv+패키지 설치 → .env 준비 → docker compose(인프라 3종) 기동 →
#   ChromaDB 초기 학습 데이터 적재 → systemd 유닛 설치(enable, 미기동)
#
# 에이전트 본체(log_watcher)는 여기서 기동하지 않는다 — docker-compose.yml
# 상단 주석 참고: systemctl/pkill 등 커널 수준 제어가 필요해 호스트 네이티브로만
# 구동하며, .env에 실제 API 키를 채운 뒤 사람이 명시적으로 시작해야 한다.
#
# 사용법:
#   bash install.sh          # 운영 설치
#   bash install.sh --dev    # + pytest 등 개발 의존성(.[dev])까지 설치
set -euo pipefail

PYTHON=${PYTHON:-python3}
VENV_DIR=".venv"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_EXTRAS=false
[ "${1:-}" = "--dev" ] && DEV_EXTRAS=true

cd "$REPO_DIR"

echo "============================================================"
echo "  Self-Healing MLOps Agent — 설치 시작"
echo "  ($REPO_DIR)"
echo "============================================================"

# ── 1. Python 버전 확인 ──────────────────────────────────────────────
echo "[1/7] Python 버전 확인..."
$PYTHON -c "import sys; assert sys.version_info >= (3,10), f'Python 3.10+ 필요 (현재: {sys.version})'"
echo "  OK: $($PYTHON --version)"

# ── 2. 가상환경 + 패키지 설치 (pyproject.toml 기준) ──────────────────
echo "[2/7] 가상환경 준비: $VENV_DIR"
if [ ! -d "$VENV_DIR" ] || [ ! -x "$VENV_DIR/bin/pip" ]; then
    rm -rf "$VENV_DIR"
    if ! $PYTHON -m venv "$VENV_DIR" 2>/tmp/venv_err_$$.log; then
        # 일부 배포판(Debian/Ubuntu 계열 등)은 ensurepip이 별도 패키지라
        # 기본 venv 생성이 pip 없이 실패한다 — get-pip.py로 직접 부트스트랩.
        cat /tmp/venv_err_$$.log
        echo "  [WARN] 기본 venv 생성 실패(ensurepip 없음으로 추정) — pip 없이 재시도 후 부트스트랩..."
        $PYTHON -m venv --without-pip "$VENV_DIR"
        curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip_$$.py
        "$VENV_DIR/bin/python" /tmp/get-pip_$$.py --quiet
        rm -f /tmp/get-pip_$$.py
    fi
    rm -f /tmp/venv_err_$$.log
    echo "  생성 완료."
else
    echo "  기존 가상환경 재사용."
fi

"$VENV_DIR/bin/pip" install --upgrade pip --quiet
if $DEV_EXTRAS; then
    "$VENV_DIR/bin/pip" install -e ".[dev]" --quiet
    echo "  설치 완료 (개발 의존성 포함)."
else
    "$VENV_DIR/bin/pip" install -e . --quiet
    echo "  설치 완료."
fi

# ── 3. .env 준비 ─────────────────────────────────────────────────────
echo "[3/7] 환경변수 파일 확인..."
NEED_ENV_FILL=false
if [ ! -f .env ]; then
    cp .env.example .env
    NEED_ENV_FILL=true
    echo "  .env.example → .env 복사 완료."
else
    echo "  .env 이미 존재 — 건너뜀."
fi

# ── 4. data 디렉터리 준비 ────────────────────────────────────────────
echo "[4/7] data/ 디렉터리 준비..."
mkdir -p data/chroma_db experiments/results
echo "  OK"

# ── 5. Docker 인프라(target-app/dashboard/approval-server) 기동 ─────
echo "[5/7] Docker 인프라 기동..."
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    docker compose up -d
    echo "  OK (dashboard: 8501, approval-server: 8000, target-app: 9000)"
else
    echo "  [SKIP] docker 또는 docker compose가 없음 — 나중에 직접"
    echo "         'docker compose up -d' 실행 필요."
fi

# ── 6. ChromaDB 초기 학습 데이터 적재 ────────────────────────────────
echo "[6/7] ChromaDB 초기 데이터 적재..."
"$VENV_DIR/bin/python" -m src.etl_vector_sync
"$VENV_DIR/bin/python" -m scripts.add_chaos_injector_signatures
"$VENV_DIR/bin/python" -m scripts.add_proactive_monitor_signatures
echo "  OK"

# ── 7. systemd 유닛 설치 (에이전트 본체 — enable만, 시작은 안 함) ────
echo "[7/7] systemd 유닛 준비..."
if command -v systemctl &>/dev/null; then
    UNIT_SRC="deploy/self-healing-agent.service"
    UNIT_TMP="$(mktemp)"
    sed "s#__REPO_DIR__#$REPO_DIR#g" "$UNIT_SRC" > "$UNIT_TMP"
    if sudo -n true 2>/dev/null; then
        sudo cp "$UNIT_TMP" /etc/systemd/system/self-healing-agent.service
        sudo systemctl daemon-reload
        sudo systemctl enable self-healing-agent >/dev/null
        echo "  OK — /etc/systemd/system/self-healing-agent.service 설치+enable 완료"
        echo "  (아직 시작 안 함 — .env를 채운 뒤 'make start' 또는"
        echo "   'sudo systemctl start self-healing-agent' 실행)"
    else
        echo "  [SKIP] passwordless sudo 없음 — 아래 명령을 직접 실행할 것:"
        echo "    sudo cp $UNIT_TMP /etc/systemd/system/self-healing-agent.service"
        echo "    sudo systemctl daemon-reload && sudo systemctl enable self-healing-agent"
    fi
else
    echo "  [SKIP] systemd 없는 환경(예: 로컬 dev 컨테이너/WSL) — 에이전트는"
    echo "         수동으로 '$VENV_DIR/bin/python -m src.log_watcher' 실행 가능."
fi

echo "============================================================"
echo "  설치 완료!"
echo "============================================================"
if $NEED_ENV_FILL; then
    echo "  !! .env 파일을 열어 최소 아래 값을 채우세요:"
    echo "       GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
fi
echo "  다음 단계: make start   (또는 위에서 SKIP된 단계를 수동 실행)"
echo "  상태 확인: make status  /  로그: make logs"
