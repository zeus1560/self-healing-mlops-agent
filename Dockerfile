FROM python:3.11-slim

WORKDIR /app

# 빌드 의존성 설치 후 캐시 정리
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        stress-ng \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 (레이어 캐시 활용)
COPY pyproject.toml .

# 런타임에 필요한 패키지만 설치 (ipex-llm, torch 등 무거운 ML 라이브러리 제외)
# Ollama는 별도 컨테이너로 분리되어 HTTP 호출만 하므로 불필요
RUN pip install --no-cache-dir \
    "chromadb>=0.5.23,<1.0" \
    "watchdog>=3.0" \
    "requests>=2.31" \
    "fastapi>=0.110" \
    "uvicorn>=0.29" \
    "streamlit>=1.33" \
    "pandas>=2.0" \
    "plotly>=5.18" \
    "python-json-logger>=2.0" \
    "scikit-learn>=1.4" \
    "python-dotenv>=1.0" \
    "psutil>=5.9"

# 소스 복사
COPY src/ src/
COPY dashboard/ dashboard/
COPY deploy/ deploy/

# 패키지 설치 (src/ import 경로 정상화)
RUN pip install --no-cache-dir -e .

# 데이터 디렉터리 생성 (볼륨 마운트 전 기본 경로 확보)
RUN mkdir -p /app/data/chroma_db

# ChromaDB ONNX 임베딩 모델 빌드 시 미리 다운로드 (컨테이너 첫 실행 지연 제거)
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()"

# 기본 실행: 로그 워처 (docker-compose에서 command로 오버라이드 가능)
CMD ["python", "-m", "src.log_watcher", "/app/data/realtime_system.log"]
