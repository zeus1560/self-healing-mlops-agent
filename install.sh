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
#   bash install.sh                 # 운영 설치 — L2 LLM 모드를 대화형으로 묻는다
#   bash install.sh --mode local    # 로컬 모드(보안 우선, 기본값) — 질문 없이 설치
#   bash install.sh --mode cloud    # 클라우드 모드(성능 우선, 로그 일부 외부 전송)
#                                   #   키는 GROQ_API_KEY 환경변수 또는 대화형 입력으로 받는다
#   bash install.sh --dev           # + pytest 등 개발 의존성(.[dev])까지 설치
#
# L2 LLM 모드는 .env의 LLM_PROVIDER(ollama|groq)로 저장된다 — README "L2 LLM 모드" 참고.
# .env에 이미 LLM_PROVIDER가 있고 --mode를 안 주면 기존 값을 그대로 둔다.
set -euo pipefail

PYTHON=${PYTHON:-python3}
VENV_DIR=".venv"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_EXTRAS=false
MODE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dev)    DEV_EXTRAS=true ;;
        --mode)   MODE="${2:-}"; shift ;;
        --mode=*) MODE="${1#--mode=}" ;;
        *) echo "알 수 없는 옵션: $1 (사용법: bash install.sh [--dev] [--mode local|cloud])"; exit 1 ;;
    esac
    shift
done
case "$MODE" in
    ""|local|cloud) ;;
    *) echo "--mode는 local 또는 cloud만 가능합니다 (입력: '$MODE')"; exit 1 ;;
esac

# .env의 KEY 값을 설정한다(활성 줄 또는 '# KEY=' 주석 줄을 교체, 없으면 추가).
# 값은 인자가 아니라 환경변수로 넘긴다 — 명령줄 인자는 ps로 다른 사용자에게 보인다.
set_env_var() {
    ENV_KEY="$1" ENV_VALUE="$2" "$PYTHON" - <<'PY'
import os, re
key, value = os.environ["ENV_KEY"], os.environ["ENV_VALUE"]
path = ".env"
lines = open(path, encoding="utf-8").read().splitlines()
pat = re.compile(rf"^\s*#?\s*{re.escape(key)}=")
for i, line in enumerate(lines):
    if pat.match(line):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}

# .env에서 KEY 활성 줄을 주석 처리한다.
comment_env_var() {
    ENV_KEY="$1" "$PYTHON" - <<'PY'
import os, re
key = os.environ["ENV_KEY"]
path = ".env"
lines = open(path, encoding="utf-8").read().splitlines()
lines = [f"# {l}" if re.match(rf"^{re.escape(key)}=", l) else l for l in lines]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}

cd "$REPO_DIR"

echo "============================================================"
echo "  Self-Healing MLOps Agent — 설치 시작"
echo "  ($REPO_DIR)"
echo "============================================================"

# ── 1. Python 버전 확인 ──────────────────────────────────────────────
echo "[1/8] Python 버전 확인..."
$PYTHON -c "import sys; assert sys.version_info >= (3,10), f'Python 3.10+ 필요 (현재: {sys.version})'"
echo "  OK: $($PYTHON --version)"

# ── 2. 가상환경 + 패키지 설치 (pyproject.toml 기준) ──────────────────
echo "[2/8] 가상환경 준비: $VENV_DIR"
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
echo "[3/8] 환경변수 파일 확인..."
NEED_ENV_FILL=false
if [ ! -f .env ]; then
    cp .env.example .env
    NEED_ENV_FILL=true
    echo "  .env.example → .env 복사 완료."
else
    echo "  .env 이미 존재 — 건너뜀."
fi

# ── 4. L2 LLM 모드 선택 ──────────────────────────────────────────────
echo "[4/8] L2 LLM 모드 선택..."
CURRENT_PROVIDER="$(grep -E '^LLM_PROVIDER=' .env | tail -1 | cut -d= -f2- || true)"
# 방금 .env.example을 복사한 경우(새 설치)엔 템플릿 기본값이라 "기존 설정"으로 보지 않는다.
if [ -z "$MODE" ] && ! $NEED_ENV_FILL && [ -n "$CURRENT_PROVIDER" ]; then
    case "$CURRENT_PROVIDER" in
        groq) MODE=cloud ;;
        *)    MODE=local ;;
    esac
    echo "  .env의 기존 설정 유지: LLM_PROVIDER=$CURRENT_PROVIDER (바꾸려면 --mode local|cloud)"
    MODE_KEEP=true
else
    MODE_KEEP=false
fi
if [ -z "$MODE" ]; then
    if [ -t 0 ]; then
        echo "  L1 캐시에 없는 새 에러를 분석할 LLM을 고르세요:"
        echo "    1) 로컬 모드 (보안 우선, 기본값) — 분석용 데이터가 서버 밖으로 나가지 않음."
        echo "       서버 안의 Ollama 사용. 새 에러에 대한 L2 제안 품질은 낮음."
        echo "    2) 클라우드 모드 (성능 우선) — 외부 LLM API(Groq) 사용."
        echo "       에러 로그 일부가 api.groq.com으로 전송됨."
        read -r -p "  선택 [1/2, 기본 1]: " CHOICE
        case "${CHOICE:-1}" in
            2) MODE=cloud ;;
            *) MODE=local ;;
        esac
    else
        MODE=local
        echo "  대화형 터미널이 아니고 --mode도 없음 — 로컬 모드로 설치합니다."
    fi
fi
if ! $MODE_KEEP; then
    if [ "$MODE" = "cloud" ]; then
        echo ""
        echo "  ⚠️  클라우드 모드: 로그 일부가 외부로 전송됩니다."
        echo "      L1 캐시에 없는 에러마다 에러 로그 원문 + 전후 최대 10줄 + 진단 명령 출력"
        echo "      (free/df/ps/ss)이 api.groq.com으로 전송됩니다. 전송 전 마스킹은 아직 없습니다."
        echo ""
        set_env_var LLM_PROVIDER groq
        comment_env_var COMPOSE_PROFILES
        if grep -qE '^GROQ_API_KEY=gsk_' .env && ! grep -qE '^GROQ_API_KEY=gsk_your_groq_api_key_here' .env; then
            echo "  GROQ_API_KEY: .env에 이미 설정됨 (값은 표시하지 않음)."
        elif [ -n "${GROQ_API_KEY:-}" ]; then
            set_env_var GROQ_API_KEY "$GROQ_API_KEY"
            echo "  GROQ_API_KEY: 환경변수 값을 .env에 저장함 (값은 표시하지 않음)."
        elif [ -t 0 ]; then
            read -r -s -p "  GROQ_API_KEY 입력 (화면에 표시되지 않음, 비우면 나중에 .env에 직접 입력): " INPUT_KEY
            echo ""
            if [ -n "$INPUT_KEY" ]; then
                set_env_var GROQ_API_KEY "$INPUT_KEY"
                echo "  GROQ_API_KEY 저장 완료."
            else
                echo "  [WARN] 키 없음 — .env에 GROQ_API_KEY를 채우기 전까지는 로컬 Ollama로만 동작합니다."
            fi
            unset INPUT_KEY
        else
            echo "  [WARN] GROQ_API_KEY 없음 — .env에 직접 채우세요(그 전까지는 로컬 Ollama로만 동작)."
        fi
        echo "  OK — 클라우드 모드(LLM_PROVIDER=groq)"
    else
        set_env_var LLM_PROVIDER ollama
        set_env_var COMPOSE_PROFILES llm
        echo "  OK — 로컬 모드(LLM_PROVIDER=ollama, Ollama 컨테이너 포함)"
    fi
fi

# ── 5. data 디렉터리 준비 ────────────────────────────────────────────
echo "[5/8] data/ 디렉터리 준비..."
mkdir -p data/chroma_db experiments/results
echo "  OK"

# ── 6. Docker 인프라(target-app/dashboard/approval-server[/ollama]) 기동 ─
# COMPOSE_PROFILES=llm(.env, 로컬 모드)이면 docker compose가 Ollama 컨테이너도 띄운다.
echo "[6/8] Docker 인프라 기동..."
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    docker compose up -d
    echo "  OK (dashboard: 8501, approval-server: 8000, target-app: 9000)"
    if [ "$MODE" = "local" ]; then
        OLLAMA_MODEL_NAME="$(grep -E '^OLLAMA_MODEL=' .env | tail -1 | cut -d= -f2- || true)"
        OLLAMA_MODEL_NAME="${OLLAMA_MODEL_NAME:-qwen2.5:0.5b}"
        echo "  Ollama 모델 받는 중: $OLLAMA_MODEL_NAME (최초 1회, 0.5b 약 400MB)..."
        if docker compose exec -T ollama ollama pull "$OLLAMA_MODEL_NAME"; then
            echo "  OK (ollama: 11434)"
        else
            echo "  [WARN] 모델 pull 실패 — 나중에 직접 실행:"
            echo "         docker compose exec ollama ollama pull $OLLAMA_MODEL_NAME"
        fi
    fi
else
    echo "  [SKIP] docker 또는 docker compose가 없음 — 나중에 직접"
    echo "         'docker compose up -d' 실행 필요."
fi

# ── 7. ChromaDB 초기 학습 데이터 적재 ────────────────────────────────
echo "[7/8] ChromaDB 초기 데이터 적재..."
"$VENV_DIR/bin/python" -m src.etl_vector_sync
"$VENV_DIR/bin/python" -m scripts.add_chaos_injector_signatures
"$VENV_DIR/bin/python" -m scripts.add_proactive_monitor_signatures
echo "  OK"

# ── 8. systemd 유닛 설치 (에이전트 본체 — enable만, 시작은 안 함) ────
echo "[8/8] systemd 유닛 준비..."
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
if [ "$MODE" = "cloud" ]; then
    echo "  L2 LLM 모드: 클라우드 모드(Groq) — 로그 일부가 외부로 전송됩니다."
else
    echo "  L2 LLM 모드: 로컬 모드(Ollama) — LLM 분석용 데이터는 서버 밖으로 나가지 않습니다."
fi
if $NEED_ENV_FILL; then
    echo "  !! .env 파일을 열어 최소 아래 값을 채우세요:"
    echo "       TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
fi
echo "  다음 단계: make start   (또는 위에서 SKIP된 단계를 수동 실행)"
echo "  상태 확인: make status  /  로그: make logs"
