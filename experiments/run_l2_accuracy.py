"""
run_l2_accuracy.py
L2(LLM) 정확도 독립 평가 실험

ChromaDB에 없는 신규 에러 50건을 L2 모델에 직접 분류하게 하여
L2 계층의 error_category 및 action_type 정확도를 측정한다.

GROQ_API_KEY가 설정되어 있으면 Groq를 사용하고(현재 운영 중인 L2 1순위와 동일),
없으면 Ollama로 폴백한다 — RAGEngine의 L2 우선순위와 동일한 규칙.
"""
import csv
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:0.5b"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
GROQ_API_URL = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")

RESULTS_DIR  = Path("experiments/results")

CATEGORIES = [
    "Out_Of_Memory", "Memory_Leak", "Network_Timeout", "DB_Connection",
    "Configuration_Error", "Permission_Denied", "Disk_Full",
    "Process_Crash", "Port_Conflict", "Auth_Error",
]

ACTION_MAP = {
    "Out_Of_Memory":      "clear_memory",
    "Memory_Leak":        "restart_service",
    "Network_Timeout":    "restart_service",
    "DB_Connection":      "restart_service",
    "Configuration_Error":"escalate_to_human",
    "Permission_Denied":  "escalate_to_human",
    "Disk_Full":          "free_disk_space",
    "Process_Crash":      "restart_service",
    "Port_Conflict":      "restart_service",
    "Auth_Error":         "escalate_to_human",
}

# ── 신규 에러셋: 카테고리별 5건, ChromaDB에 없는 고유 시나리오 ──────
NOVEL_ERRORS = [
    # Out_Of_Memory (5건)
    {"log": "FATAL: Torch CUDA out of memory. Tried to allocate 3.50 GiB (GPU 0; 10.76 GiB total capacity; 8.92 GiB already allocated). Consider reducing batch_size.", "category": "Out_Of_Memory"},
    {"log": "java.lang.OutOfMemoryError: GC overhead limit exceeded at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:412)", "category": "Out_Of_Memory"},
    {"log": "MemoryError: Unable to allocate 14.2 GiB for array with shape (1920000000,) and data type float64", "category": "Out_Of_Memory"},
    {"log": "ERROR kernel: Out of memory: Kill process 18743 (gunicorn) score 892 or sacrifice child. Killed process 18743 total-vm:9845328kB, anon-rss:7612400kB", "category": "Out_Of_Memory"},
    {"log": "RuntimeError: CUDA out of memory. Tried to allocate 512 MiB. GPU 0 has a total capacity of 8.00 GiB of which 312 MiB is free.", "category": "Out_Of_Memory"},

    # Memory_Leak (5건)
    {"log": "WARN  MemoryManager: RSS memory 14.8GB exceeds soft limit 12GB. Heap growth detected over last 2h: +340MB/min. Possible memory leak in DataLoader workers.", "category": "Memory_Leak"},
    {"log": "WARNING: python3 process memory usage: 11.2 GB (was 2.1 GB 3 hours ago). GC collections: gen0=142891 gen1=312 gen2=0. Leak suspected in cache layer.", "category": "Memory_Leak"},
    {"log": "ALERT: celery worker PID 7821 resident set size grew from 512MB to 9.8GB over 6 hours without GC release. Restarting worker recommended.", "category": "Memory_Leak"},
    {"log": "HeapDump triggered: live objects count increased by 2.3M in last 30min. Dominant type: byte[] (87%). Possible off-heap memory leak in netty pipeline.", "category": "Memory_Leak"},
    {"log": "mlflow tracking server: memory increased 50MB/request, no release observed. Top allocator: artifact cache (LRUCache unbounded). OOM expected within 2h.", "category": "Memory_Leak"},

    # Network_Timeout (5건)
    {"log": "requests.exceptions.ConnectTimeout: HTTPSConnectionPool(host='s3.us-east-1.amazonaws.com', port=443): Max retries exceeded. Connect timeout=30s", "category": "Network_Timeout"},
    {"log": "ERROR grpc: connection to inference-server:50051 timeout after 15000ms. Retries=3 exhausted. Last error: DeadlineExceeded status=4", "category": "Network_Timeout"},
    {"log": "TimeoutError: Ray remote call to worker@192.168.10.45 did not complete within 120s. Task: preprocess_batch. Worker appears unresponsive.", "category": "Network_Timeout"},
    {"log": "socket.timeout: timed out waiting for Kafka broker response (bootstrap.servers=kafka:9092, timeout.ms=30000). Topic: ml-predictions offset lag: 84293", "category": "Network_Timeout"},
    {"log": "urllib3.exceptions.ReadTimeoutError: HTTPConnectionPool(host='feature-store', port=8080): Read timed out. (read timeout=45) after sending GET /features/batch", "category": "Network_Timeout"},

    # DB_Connection (5건)
    {"log": "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) SSL connection has been closed unexpectedly. host=pgbouncer port=6432 dbname=mlops_prod", "category": "DB_Connection"},
    {"log": "ERROR [connection-pool] All 20 connections in pool exhausted. Waiting threads: 47. PostgreSQL max_connections=100 (85 used by other services).", "category": "DB_Connection"},
    {"log": "pymongo.errors.ServerSelectionTimeoutError: mongodb://mongo-primary:27017: [Errno 111] Connection refused, Timeout: 30s, Topology: ReplicaSetNoPrimary", "category": "DB_Connection"},
    {"log": "redis.exceptions.ConnectionError: Error 104 connecting to redis-sentinel:26379. Connection reset by peer. Sentinel failover in progress.", "category": "DB_Connection"},
    {"log": "ERROR Cassandra: All host(s) tried for query failed. Last host tried: 10.0.1.15:9042 ([Errno 110] Connection timed out). Keyspace: ml_features", "category": "DB_Connection"},

    # Configuration_Error (5건)
    {"log": "yaml.scanner.ScannerError: mapping values are not allowed here in 'config/training.yaml', line 47, column 18. Check indentation near 'learning_rate:'", "category": "Configuration_Error"},
    {"log": "ConfigurationError: Invalid value for BATCH_SIZE: 'auto' (expected int). Check environment variable or config/defaults.yaml batch_size field.", "category": "Configuration_Error"},
    {"log": "ERROR: Failed to parse Hydra config. OmegaConf error at model.architecture.layers[2]: value '256x' cannot be converted to int", "category": "Configuration_Error"},
    {"log": "toml.decoder.TomlDecodeError: Found invalid character in key name: ' '. (line 23 column 5 char 412) in pyproject.toml [tool.mlflow] section", "category": "Configuration_Error"},
    {"log": "jsonschema.exceptions.ValidationError: 'dropout_rate' is a required property in schema 'ModelConfig'. Check model_config.json missing field.", "category": "Configuration_Error"},

    # Permission_Denied (5건)
    {"log": "PermissionError: [Errno 13] Permission denied: '/mnt/nfs/checkpoints/model_v3/epoch_45.ckpt'. Current user: mlops-svc (uid=1001). Required: write on /mnt/nfs", "category": "Permission_Denied"},
    {"log": "ERROR s3: AccessDenied: User: arn:aws:iam::123456789:role/ml-training is not authorized to perform: s3:PutObject on resource: arn:aws:s3:::prod-models/*", "category": "Permission_Denied"},
    {"log": "subprocess.CalledProcessError: EACCES: permission denied, open '/var/log/mlflow/tracking.log'. Process uid=1002 requires group 'mlflow-log' membership.", "category": "Permission_Denied"},
    {"log": "kubectl: Error from server (Forbidden): pods is forbidden: User 'system:serviceaccount:ml-team:trainer' cannot create resource 'pods' in namespace 'gpu-pool'", "category": "Permission_Denied"},
    {"log": "ERROR docker: Got permission denied while trying to connect to Docker daemon socket /var/run/docker.sock. Add user 'mlops' to 'docker' group.", "category": "Permission_Denied"},

    # Disk_Full (5건)
    {"log": "OSError: [Errno 28] No space left on device: '/data/mlflow/artifacts/run_a3f9b2/checkpoints/epoch_200.pt'. Disk usage: /data 99.8% (1.8T/1.8T)", "category": "Disk_Full"},
    {"log": "ERROR: Cannot write tensorboard event file. [Errno 28] No space left on device at /tmp/tensorboard_logs. Free: 0 bytes on /tmp (tmpfs 8G full).", "category": "Disk_Full"},
    {"log": "FATAL: PostgreSQL could not write to file 'pg_wal/000000010000003A': No space left on device. WAL segment creation failed. Database halted.", "category": "Disk_Full"},
    {"log": "docker: Error response from daemon: failed to create layer: apply layer: ApplyLayer: write /var/lib/docker/overlay2: no space left on device.", "category": "Disk_Full"},
    {"log": "DiskFullError: Spark shuffle write failed on executor 12. Path: /local/disk1/spark-shuffle/. Filesystem 97% full (485G/500G). Executor will be removed.", "category": "Disk_Full"},

    # Process_Crash (5건)
    {"log": "ERROR: Training worker PID 23841 terminated with signal 11 (SIGSEGV). Core dumped to /tmp/core.23841. Last op: torch::autograd::AccumulateGrad", "category": "Process_Crash"},
    {"log": "CRITICAL: Celery worker process exited with code 137 (SIGKILL/OOM). Task ml_inference.predict_batch was lost. Re-queuing with retry=2.", "category": "Process_Crash"},
    {"log": "fatal error: unexpected signal during runtime execution. SIGBUS at PC=0x7f3a9c000000 sp=0x7ffe12340000. Go runtime crashed in model serving goroutine.", "category": "Process_Crash"},
    {"log": "ERROR supervisor: gunicorn worker with pid 9923 died. Stacktrace: Segmentation fault (core dumped). Respawning. Crashes in last 1h: 5", "category": "Process_Crash"},
    {"log": "WATCHDOG: Process 'model-server' (PID 4412) killed — health check failed 3 consecutive times. Exit code: -9. Restarting with backoff 30s.", "category": "Process_Crash"},

    # Port_Conflict (5건)
    {"log": "OSError: [Errno 98] Address already in use. Failed to bind TensorBoard to 0.0.0.0:6006. Port occupied by PID 8823 (python3 train.py --tensorboard)", "category": "Port_Conflict"},
    {"log": "ERROR: MLflow UI failed to start: [Errno 98] EADDRINUSE: address already in use :::5000. Try: mlflow ui --port 5001", "category": "Port_Conflict"},
    {"log": "uvicorn.error: [Errno 98] error while attempting to bind on address ('0.0.0.0', 8080): address already in use. Previous instance still running?", "category": "Port_Conflict"},
    {"log": "FATAL: Ray head node cannot start GCS on port 6379: bind: address already in use. Redis may already be occupying port 6379.", "category": "Port_Conflict"},
    {"log": "Error: Jupyter Lab port 8888 is already in use. Kill the process using: lsof -ti:8888 | xargs kill -9, then restart.", "category": "Port_Conflict"},

    # Auth_Error (5건)
    {"log": "AuthenticationError: Weights & Biases API key invalid or expired. Token: 'wand_xxxxxx...'. Re-authenticate with: wandb login --relogin", "category": "Auth_Error"},
    {"log": "ERROR mlflow: 401 Unauthorized: Bearer token expired for tracking server https://mlflow.internal. Re-run: mlflow.set_tracking_uri() with fresh token.", "category": "Auth_Error"},
    {"log": "google.auth.exceptions.TransportError: 403 Forbidden. Service account ml-trainer@project.iam.gserviceaccount.com lacks role roles/storage.objectAdmin on bucket gs://ml-data", "category": "Auth_Error"},
    {"log": "JWTError: Signature verification failed. Token issued at 2026-05-10T09:00:00Z expired at 2026-05-10T10:00:00Z. Current time: 2026-05-22T14:30:00Z. Re-login required.", "category": "Auth_Error"},
    {"log": "ERROR: Vault token renewal failed: permission denied. Policy 'ml-secrets-read' does not allow token renewal. Contact admin to re-issue token.", "category": "Auth_Error"},
]

CLASSIFY_PROMPT = """You are a log classification expert for MLOps systems.
Given an error log, output EXACTLY this JSON format and nothing else:
{{"category": "<CATEGORY>", "action": "<ACTION>"}}

Categories: Out_Of_Memory, Memory_Leak, Network_Timeout, DB_Connection, Configuration_Error, Permission_Denied, Disk_Full, Process_Crash, Port_Conflict, Auth_Error

Actions: clear_memory, restart_service, free_disk_space, escalate_to_human

Error log:
{log}

JSON:"""


def _parse_prediction(raw: str, latency_ms: float) -> tuple[str, str, float]:
    """LLM 원문 응답에서 {"category": ..., "action": ...} JSON을 추출한다."""
    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            obj = json.loads(raw[start:end])
            cat = obj.get("category", "").strip()
            act = obj.get("action", "").strip()
            return cat, act, latency_ms
    except Exception:
        pass
    return "PARSE_ERROR", "PARSE_ERROR", latency_ms


def call_ollama(log_text: str) -> tuple[str, str, float]:
    """Ollama 호출 후 (pred_category, pred_action, latency_ms) 반환."""
    prompt = CLASSIFY_PROMPT.format(log=log_text[:600])
    payload = json.dumps({
        "model":  OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 64},
    }).encode()

    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        latency_ms = (time.perf_counter() - t0) * 1000
        raw = result.get("response", "").strip()
    except Exception:
        latency_ms = (time.perf_counter() - t0) * 1000
        return "ERROR", "ERROR", latency_ms

    return _parse_prediction(raw, latency_ms)


def call_groq(log_text: str) -> tuple[str, str, float]:
    """Groq 호출 후 (pred_category, pred_action, latency_ms) 반환. 운영 L2 1순위와 동일한 백엔드."""
    prompt = CLASSIFY_PROMPT.format(log=log_text[:600])
    payload = json.dumps({
        "model":            GROQ_MODEL,
        "messages":         [{"role": "user", "content": prompt}],
        "temperature":      0.0,
        "max_tokens":       64,
        # qwen3 계열의 <think> 체인 생성을 비활성화해 max_tokens 내 응답 유실을 방지 (llm_engine.py와 동일)
        "reasoning_effort": "none",
    }).encode()

    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            GROQ_API_URL, data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {GROQ_API_KEY}",
                # 기본 urllib UA는 Cloudflare에 차단(1010)되므로 명시 지정 (llm_engine.py와 동일)
                "User-Agent":    "self-healing-mlops-agent/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        latency_ms = (time.perf_counter() - t0) * 1000
        raw = result["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        body = e.read().decode(errors="ignore")[:200]
        print(f"    [Groq HTTP {e.code}] {body}")
        return "ERROR", "ERROR", latency_ms
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        print(f"    [Groq 예외] {e!r}")
        return "ERROR", "ERROR", latency_ms

    return _parse_prediction(raw, latency_ms)


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if GROQ_API_KEY:
        backend, model, call_fn = "groq", GROQ_MODEL, call_groq
    else:
        backend, model, call_fn = "ollama", OLLAMA_MODEL, call_ollama

    print("=" * 65)
    print("  L2(LLM) 정확도 독립 평가 — 신규 에러 50건")
    print("=" * 65)
    print(f"  백엔드: {backend} ({model}) | 에러 수: {len(NOVEL_ERRORS)}건\n")

    records = []
    cat_correct = 0
    act_correct = 0
    latencies   = []

    # Groq 무료 티어 레이트리밋(30 RPM)에 안 걸리도록 순차 호출 간격을 둔다.
    GROQ_CALL_INTERVAL_SEC = 2.2
    if backend == "groq":
        print(f"  (레이트리밋 회피를 위해 요청 간 {GROQ_CALL_INTERVAL_SEC}초 간격 — 총 약 {GROQ_CALL_INTERVAL_SEC * len(NOVEL_ERRORS):.0f}초 소요 예상)\n")

    print(f"{'#':>3} {'Category':>20} {'Pred':>20} {'Act✓':>5} {'ms':>7}")
    print("-" * 65)

    for i, item in enumerate(NOVEL_ERRORS):
        true_cat = item["category"]
        true_act = ACTION_MAP[true_cat]

        if backend == "groq" and i > 0:
            time.sleep(GROQ_CALL_INTERVAL_SEC)

        pred_cat, pred_act, lat = call_fn(item["log"])
        latencies.append(lat)

        cat_ok = (pred_cat == true_cat)
        act_ok = (pred_act == true_act)
        if cat_ok:
            cat_correct += 1
        if act_ok:
            act_correct += 1

        mark = "✓" if cat_ok else "✗"
        print(f"{i+1:>3} {true_cat:>20} {pred_cat:>20} {'✓' if act_ok else '✗':>5} {lat:>6.0f}ms  {mark}")

        records.append({
            "idx":       i + 1,
            "true_cat":  true_cat,
            "pred_cat":  pred_cat,
            "true_act":  true_act,
            "pred_act":  pred_act,
            "cat_correct": cat_ok,
            "act_correct": act_ok,
            "latency_ms":  round(lat, 1),
        })

    n = len(NOVEL_ERRORS)
    cat_acc = cat_correct / n
    act_acc = act_correct / n
    avg_lat = sum(latencies) / len(latencies)

    # 카테고리별 정확도
    from collections import defaultdict
    cat_stats: dict[str, list] = defaultdict(list)
    for r in records:
        cat_stats[r["true_cat"]].append(r["cat_correct"])

    print("\n" + "=" * 65)
    print("  카테고리별 정확도")
    print("=" * 65)
    for cat in CATEGORIES:
        vals = cat_stats.get(cat, [])
        acc  = sum(vals) / len(vals) if vals else 0
        bar  = "█" * int(acc * 20)
        print(f"  {cat:<22} {acc*100:>5.1f}%  {bar}")

    print("\n" + "=" * 65)
    print("  종합 결과 (n=50)")
    print("=" * 65)
    print(f"  Category Accuracy : {cat_acc*100:.1f}%  ({cat_correct}/{n})")
    print(f"  Action Accuracy   : {act_acc*100:.1f}%  ({act_correct}/{n})")
    print(f"  평균 응답 지연     : {avg_lat:.0f}ms")
    print(f"  L1 대비 속도       : L1 ~189ms → L2 {avg_lat:.0f}ms ({avg_lat/189:.1f}x 느림)")
    print("=" * 65)

    # CSV 저장 (백엔드별 파일 분리 — 기존 Ollama 베이스라인을 덮어쓰지 않음)
    out_path = RESULTS_DIR / f"l2_accuracy_results_{backend}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)

    # 요약 JSON 저장
    summary = {
        "backend":         backend,
        "model":           model,
        "n_samples":       n,
        "cat_accuracy":    round(cat_acc, 4),
        "act_accuracy":    round(act_acc, 4),
        "avg_latency_ms":  round(avg_lat, 1),
        "l1_latency_ms":   189.0,
        "slowdown_x":      round(avg_lat / 189.0, 1),
        "per_category":    {
            cat: round(sum(v)/len(v), 4) if v else 0
            for cat, v in cat_stats.items()
        },
    }
    summary_path = RESULTS_DIR / f"l2_accuracy_summary_{backend}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n  CSV  저장: {out_path}")
    print(f"  JSON 저장: {summary_path}")
    return summary


if __name__ == "__main__":
    main()
