"""
다양한 syslog 형식 학습 데이터 적재 스크립트
==============================================
10개 에러 카테고리 × 30개 = 300개 syslog 형식 훈련 데이터를 ChromaDB에 적재합니다.

기존 ETL 데이터(GitHub Issues 마크다운 형식)와 달리,
실제 Linux/Cloud 서비스 로그 형식으로 작성되어 syslog 쿼리와의 의미적 거리가 낮습니다.

실행:
    python scripts/load_syslog_train.py           # 적재
    python scripts/load_syslog_train.py --verify  # 각 카테고리 top-1 검색 검증
    python scripts/load_syslog_train.py --remove  # 이 스크립트로 추가한 항목만 삭제
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings
from rich import print as rprint
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

CHROMA_DIR      = str(Path(__file__).parent.parent / "data" / "chroma_db")
COLLECTION_NAME = "error_playbook_vectors"
ID_PREFIX       = "syslog_v1_"

# ── 학습 데이터 정의 ─────────────────────────────────────────────────────────
# 포맷: 실제 syslog/journald 형식
# 서비스 커버리지: nginx, apache, mysql, postgres, redis, mongodb,
#                  docker, k8s, celery, gunicorn, django, fastapi,
#                  systemd, kernel, java, python, node.js

SYSLOG_ENTRIES = [

    # ══════════════════════════════════════════════════════════════════════════
    # 1. Out_Of_Memory (30개) — 다양한 프로세스에서 OOM 발생
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "kernel: Out of memory: Kill process 12483 (java) score 921 or sacrifice child",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: Killed process 8821 (python3) total-vm:4096000kB, anon-rss:3801200kB",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "CRITICAL redis[3391]: Can't save in background: fork: Cannot allocate memory",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR mysql[1204]: Out of memory (Needed 134217728 bytes); check if mysqld or some other process uses all available memory",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/docker,task=nginx,pid=9012,uid=33",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "CRITICAL celery[5501]: worker: OutOfMemory — heap exhausted, terminating worker pool",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "gunicorn[7743]: CRITICAL — Worker with pid 7750 exited due to signal SIGKILL (OOM killer)",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: MemAvailable: 12344 kB — critically low, OOM imminent for node exporter",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR postgres[2211]: out of memory for query result, freeing 45% of tuple memory",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kubelet[831]: Evicted pod ml-worker due to memory pressure: MemoryAvailable=48Mi, threshold=100Mi",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR fastapi[6600]: MemoryError — unable to allocate 2.1GB for model inference batch",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "docker: container ml-serving killed — OOM, memory limit 4Gi exceeded (used 4.3Gi)",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "WARN systemd-oomd[1]: Memory pressure exceeded limit 60% — killing cgroup /system.slice/api.service",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "node[4411]: FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed — JavaScript heap out of memory",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: Out of memory: Kill process 19933 (mongod) score 874 — mongodb instance terminated",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR spark-executor[3001]: ExecutorLostFailure: java.lang.OutOfMemoryError: GC overhead limit exceeded",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "CRITICAL api-server[8800]: std::bad_alloc — failed to allocate 536870912 bytes for request buffer",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: lowmem_reserve ratio threshold crossed — available: 38MB / 16384MB",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "prometheus[2109]: WARN tsdb/head.go: memory mapped chunks OOM, reducing retention",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR logstash[7700]: java.lang.OutOfMemoryError: Java heap space at pipeline worker",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: Memory cgroup out of memory: Killed process 6621 (python) in cgroup /kubepods",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "CRITICAL nginx[4422]: worker_processes memory limit exceeded — killing worker 4430",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "flask[9910]: MemoryError: cannot allocate memory in static TLS block",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR kafka[5501]: java.lang.OutOfMemoryError: Direct buffer memory exhausted",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: kswapd0: page allocation failure. order:4, mode:0x40cc0, gfp_mask:GFP_KERNEL",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "containerd[911]: container runtime ran out of memory, pod evicted: ml-training",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "ERROR rabbitmq[3309]: vm_memory_high_watermark exceeded (0.92) — publishers blocked",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "CRITICAL airflow-worker[6614]: MemoryError in task execution — DAG ml_pipeline run failed",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "jvm[8801]: GC overhead limit exceeded: 98% of CPU time spent on GC, heap 7.9GB / 8GB",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},
    {"log_text": "kernel: oom_reaper: reaped process 14422 (torch_worker), now anon-rss:0kB, file-rss:0kB",
     "error_category": "Out_Of_Memory", "action_type": "clear_memory"},

    # ══════════════════════════════════════════════════════════════════════════
    # 2. Memory_Leak (30개) — 다양한 서비스의 메모리 누수
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "WARN monitor: python3 worker[4401] heap growing +95MB/hour — memory leak suspected",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR nginx[2201]: worker process memory grew from 128MB to 3.8GB over 6h — leak detected",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL java-app[5503]: heap utilization 97% after full GC — memory leak in servlet pool",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN redis[3391]: rss_overhead_ratio 2.85 — RSS memory 5.7GB vs used_memory 2.0GB, fragmentation leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR celery[6600]: memory growth detected — worker RSS 4.1GB after 12h, initial was 210MB",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL node[8811]: memory leak alert — heap used 1.82GB / 1.98GB, V8 heap growing unbounded",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN docker-stats: container api-server memory: 3.92GB / 4.00GB — possible leak, restarting",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR gunicorn[7743]: worker[7750] RSS grew to 2.9GB — suspected unclosed DB connection leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL prometheus[2109]: go runtime memory: alloc=4.2GB, sys=5.1GB — heap growing unbounded",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN rabbitmq[3309]: binary memory 1.9GB — binary leak detected, forcing GC",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR mongo[4450]: wiredTiger cache: 7.8GB / 8.0GB — cache pressure, possible leak in aggregation cursor",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL airflow[6614]: scheduler memory leak — RSS climbing 250MB/hour, dag parsing loop",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN fastapi[6600]: tracemalloc: top memory consumer growing +45MB/request — response serializer leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR nginx[4422]: $request_time growing: session tracking hash uncleaned for 48h",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL logstash[7700]: JVM old gen 99% — long-lived objects accumulating, GC unable to reclaim",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN mysql[1204]: Innodb_buffer_pool_bytes_dirty increasing without flush — buffer pool leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR flask[9910]: tracemalloc top: linecache.py 820MB — repeated template reload not freed",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL k8s-pod ml-server: container memory 3.95GB / 4.00GB OOMKill threshold — leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN elasticsearch[5501]: heap used 29.8GB / 30GB — fielddata cache not evicting, memory leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR tornado[8801]: fd leak combined with memory — open file descriptors 65500 / 65536",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL spark-driver[3001]: memory leak in broadcast variable — accumulators not cleaned: 48GB",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN grpc-server[7700]: channel pool memory growing +120MB/min — keep-alive leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR haproxy[2211]: memory leak in SSL session cache — sessions not expired after timeout",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL tensorflow-serving[9900]: model hot-reload memory not freed — RSS grew 12GB in 2h",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN php-fpm[4411]: memory_limit nearly reached — process 4422 using 490MB / 512MB, leak detected",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR containerd[911]: memory pressure: cgroup ml-trainer limit approaching, suspected leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL sidecar-proxy[3300]: envoy memory leak in downstream connection tracking: 3.2GB RSS",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "WARN java-app[5503]: PhantomReference queue not draining — possible native memory leak",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "ERROR gpu-service[8821]: CUDA context memory not released after inference — cumulative leak 18GB",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},
    {"log_text": "CRITICAL monitor: process api-server RSS grew 450MB → 8.9GB over 24h — intervention required",
     "error_category": "Memory_Leak", "action_type": "clear_memory"},

    # ══════════════════════════════════════════════════════════════════════════
    # 3. Disk_Full (30개) — 다양한 서비스/파티션 디스크 풀
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR postgres[2211]: could not write to file 'pg_wal/000000010000012B00000001': No space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL systemd[1]: /dev/sda1 at 100% — write I/O blocked, journald dropping messages",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR mysql[1204]: Got error 28 from storage engine (No space left on device) writing to /var/lib/mysql",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR nginx[4422]: open() '/var/log/nginx/access.log' failed (28: No space left on device)",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL docker: failed to write layer to /var/lib/docker — No space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR redis[3391]: Failed opening the RDB file dump.rdb: No space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "WARN df: /var/log filesystem 97% full (threshold 85%) — disk cleanup required",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR elasticsearch[5501]: IOException: No space left on device writing to /data/indices",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL kafka[5501]: IOException writing to log segment — /var/kafka-logs: No space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR loki[7700]: chunk flush failed: write /data/loki/chunks: no space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL grafana[8800]: failed to save dashboard: database disk image is malformed, disk full",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR airflow[6614]: task log write failed: [Errno 28] No space left on device: '/opt/airflow/logs'",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "WARN logrotate: disk usage 95% on /dev/sdb1 — log rotation triggered, 23GB remaining",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR prometheus[2109]: opening chunk segment failed: no space left on device /prometheus",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL ceph[4450]: OSD 7 FULL — refusing writes, disk capacity exceeded on /dev/sdc",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR rsync: [sender] write error: No space left on device (28) — backup aborted",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL tmpfs: /tmp at 100% (8GB) — application temp files not cleaned up",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR containerd[911]: image pull failed: no space left on device extracting layer to /var/lib/containerd",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "WARN influxdb[3309]: write error: engine: cache-max-memory-size exceeded, disk: no space left",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR mongo[4450]: WiredTiger error: posix_fallocate: [28] No space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL etcd[5601]: failed to create snapshot: write /var/lib/etcd/snap: no space left",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR syslog: journal: /var/log/journal partition full — oldest logs will be dropped",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR mlflow[6600]: artifact upload failed: OSError: [Errno 28] No space left on device: /mlruns",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "WARN zookeeper[3310]: disk usage for /data/zookeeper: 98% — transaction log cleanup needed",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL jenkins[8801]: unable to write build log — No space left on device: /var/jenkins_home",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR gitlab-runner[7700]: cache archiving failed: write /cache: no space left on device",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL k8s-node: node disk pressure condition True — /dev/sda 99% full, pod scheduling suspended",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "ERROR apache[4411]: AH00086: pid file /var/run/apache2/apache2.pid overwrite attempt denied: disk full",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "WARN filebeat[9900]: harvester failed to read file: no space left on device /var/log/app",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},
    {"log_text": "CRITICAL nfs-server[2200]: write failed for client 10.0.1.5: no space left on exported volume /exports",
     "error_category": "Disk_Full", "action_type": "execute_rule_command", "command": "journalctl --vacuum-size 1G"},

    # ══════════════════════════════════════════════════════════════════════════
    # 4. Process_Crash (30개) — 다양한 서비스 프로세스 비정상 종료
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR systemd[1]: nginx.service: Main process exited, code=dumped, status=11/SEGV",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL mysql[1204]: mysqld: Segmentation fault (core dumped) — service terminated",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR systemd[1]: redis.service: Control process exited, code=killed, status=9/KILL",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL apache2[4411]: child pid 4419 exit signal Segmentation fault (11) — worker crashed",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR gunicorn[7743]: worker exited with code 1 — unexpected signal 6 (SIGABRT)",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL celery[5501]: task worker terminated unexpectedly with signal 11, restarting...",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR systemd[1]: postgres.service: start operation timed out, terminated",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL java-app[5503]: JVM crashed — hs_err_pid5503.log generated, SIGSEGV in GC thread",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR containerd[911]: container exited with non-zero code: ml-serving crashed (exit code 139)",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL mongo[4450]: mongod got signal 11 — backtrace available, process terminated",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR node[4411]: process exited with code: 1 — uncaughtException: Cannot read property of undefined",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL systemd[1]: haproxy.service: Failed with result 'core-dump'",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR python3[9910]: Fatal Python error: Segmentation fault — core dumped",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL kafka[5501]: FATAL Uncaught exception in main thread — broker shut down due to fatal error",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR systemd[1]: rabbitmq-server.service: Watchdog timeout exceeded, killing",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL fastapi[6600]: worker[6605] died with signal 11 SIGSEGV during request processing",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR php-fpm[4411]: child 4425 exited with code 255 (SIGSEGV) after processing request",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL systemd[1]: etcd.service: Main process exited, code=exited, status=2/INVALIDARGUMENT",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR prometheus[2109]: panic: runtime error: index out of range — process terminated",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL fluentd[7700]: unexpected error: Errno::ENOBUFS — process killed by supervisor",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR systemd[1]: grafana-server.service: Failed to execute: Killed",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL spark-executor[3001]: executor lost: ExecutorDeadException — JVM crash detected",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR tensorflow-serving[9900]: Segmentation fault (core dumped) — model server terminated",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL systemd[1]: consul.service: Main process exited, code=signal, status=6/ABRT",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR envoy[3300]: [critical] caught SIGSEGV — proxy process crashed, upstream requests dropped",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL airflow-scheduler[6614]: process died unexpectedly — DagFileProcessorManager crashed",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR loki[7700]: goroutine panic: runtime error: invalid memory address — process restarting",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL nginx[4422]: all workers crashed — upstream unavailable: service recovering",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "ERROR docker[911]: container ml-api exited with code 137 (OOM/kill signal) — restart policy triggered",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},
    {"log_text": "CRITICAL systemd[1]: api-gateway.service: Start request repeated too quickly, unit failed",
     "error_category": "Process_Crash", "action_type": "restart_service", "target_process": "rsyslog"},

    # ══════════════════════════════════════════════════════════════════════════
    # 5. Network_Timeout (30개) — 다양한 네트워크 연결 타임아웃
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR http-client[8800]: Connection timeout after 30000ms — upstream api.internal:443 unreachable",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL nginx[4422]: upstream timed out (110: Connection timed out) while reading response header from upstream",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR grpc[7700]: rpc error: code = DeadlineExceeded desc = context deadline exceeded — target: inference-svc:9000",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN haproxy[2211]: backend app_servers: 3/5 servers DOWN — health check timeout 5000ms exceeded",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR requests[9910]: HTTPSConnectionPool: Read timed out (read timeout=10) for https://api.external.com",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL load-balancer[2200]: health check failed — 10.0.1.100:8080 timeout after 3000ms, marking DOWN",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR java-app[5503]: SocketTimeoutException: Read timed out for connection to kafka:9092",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN envoy[3300]: upstream request timeout — cluster ml-backend, timeout 15s exceeded",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR celery[5501]: billiard.exceptions.TimeLimitExceeded: connection timeout to redis:6379 exceeded 60s",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL istio-proxy[3300]: upstream_request_timeout: stream reset by peer, host 10.0.2.50:5000",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR apache[4411]: AH01102: error reading status line from remote server 10.0.1.10:8080, timeout",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN kafka[5501]: NetworkClient: disconnected from node 1 (10.0.3.11:9092) due to timeout",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR fastapi[6600]: httpx.ReadTimeout: timed out waiting for data from 10.0.4.20:8443",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL consul[5601]: health check timed out for service api-server — marking critical",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR elasticsearch[5501]: master not discovered yet — cluster state timeout 30s exceeded",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN prometheus[2109]: scrape timeout for job api — target 10.0.1.55:9090 not responding (10s)",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR sqlalchemy[9910]: TimeoutError: QueuePool limit reached, connection attempt timed out after 10s",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL k8s: readiness probe failed for pod api-server — TCP timeout on :8080 for 30s",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR node[4411]: Error: ETIMEDOUT — connection to postgres:5432 timed out after 5000ms",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN curl: (28) Operation timed out after 30001 milliseconds — GET http://svc.internal/health",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR rabbit-client[3309]: AMQP connection closed: TCP connection timeout 60s to rabbitmq:5672",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL traefik[2211]: dial tcp 10.0.2.30:3000 i/o timeout — backend grafana unreachable",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR airflow[6614]: requests.exceptions.Timeout: HTTPConnectionPool host=webserver port=8080, timeout=10",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN fluentd[7700]: failed to flush events to elasticsearch: Net::ReadTimeout occurred",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR grpc[7700]: connection to 10.0.5.10:50051 timeout: deadline_exceeded after 5s",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL jaeger[9900]: reporter: cannot save span to collector, timeout: dial tcp 10.0.6.1:14250",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR celery[5501]: redis.exceptions.TimeoutError: Timeout reading from socket to redis:6379",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN nginx[4422]: upstream keepalive timeout — backend pool exhausted, new connect timeout 5s",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR boto3[9910]: botocore.exceptions.ConnectTimeoutError: Connect timeout on endpoint URL S3",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL istio[3300]: 504 Gateway Timeout — upstream response deadline exceeded for /api/predict",
     "error_category": "Network_Timeout", "action_type": "execute_rule_command", "command": "ss -tuln"},

    # ══════════════════════════════════════════════════════════════════════════
    # 6. DB_Connection (30개) — 다양한 DB 연결 실패/풀 고갈
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR sqlalchemy[9910]: QueuePool limit of size 20 overflow 10 reached, connection refused",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL postgres[2211]: FATAL: remaining connection slots are reserved for non-replication superuser",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR mysql[1204]: Too many connections — max_connections=200 exhausted",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN redis[3391]: max number of clients reached 10000 — connection refused from 10.0.1.1",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR mongo[4450]: connection pool exhausted: pool size 100, waitQueueSize 500",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL api-server[8800]: Database connection pool exhausted — all 50 connections active",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR django[9910]: django.db.OperationalError: could not connect to server: Connection refused",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN pg-bouncer[2212]: max_client_conn 200 reached — new connections rejected from app-server",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR cassandra[4450]: NoHostAvailable — all hosts tried are unreachable: connection refused",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL elasticsearch[5501]: cluster health RED — unassigned shards, write connections rejected",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR java-app[5503]: com.mysql.jdbc.exceptions.jdbc4.CommunicationsException: Communications link failure",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN influxdb[3309]: max-concurrent-write-limit 10 exceeded — writes queued",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR api-server[8800]: db query failed: context deadline exceeded, connection pool wait 30s",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL clickhouse[5602]: DB::Exception: Too many simultaneous connections (201 > max 200)",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR fastapi[6600]: asyncpg.TooManyConnectionsError: sorry, too many clients already",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN postgres[2211]: connection pool at 95% capacity — remaining 5 slots reserved",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR node[4411]: Error: connect ECONNREFUSED 127.0.0.1:5432 — postgres not accepting connections",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL druid[5603]: unable to acquire connection: pool exhausted, maxSize=50 active=50",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR celery[5501]: kombu.exceptions.OperationalError: redis max connections exceeded",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN tidb[5604]: connection count 4500 approaching limit 5000 — throttling new connections",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR flask[9910]: sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) server closed the connection",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL neo4j[5605]: Transaction timeout — maximum connection pool size 100 exceeded",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR prometheus[2109]: failed to query remote storage: post error: connection pool full",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN sqlalchemy[9910]: connection pool pre-ping failed — DB connection dropped, reconnecting",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR aiohttp[6600]: aiohttp.ServerDisconnectedError: Server disconnected — postgres restarted",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL vitess[5606]: vttablet: connection pool saturation 100% — queries queued 5000ms",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR django[9910]: OperationalError: FATAL: connection limit exceeded for database mydb",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "WARN hikari[5503]: HikariPool-1 timed out waiting for connection after 30000ms — pool exhausted",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "ERROR typeorm[4411]: QueryFailedError: could not obtain lock on relation — DB under heavy load",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},
    {"log_text": "CRITICAL postgres-pool: active_connections=100/100 — pool saturated, requests queued for 45s",
     "error_category": "DB_Connection", "action_type": "execute_rule_command", "command": "free -h"},

    # ══════════════════════════════════════════════════════════════════════════
    # 7. Auth_Error (30개) — 다양한 인증/권한 실패
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR sshd[2200]: Failed password for root from 192.168.1.15 port 44392 ssh2 — 5th attempt",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL auth-service[6600]: jwt.ExpiredSignatureError — token expired 3600s ago for user_id=4421",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR nginx[4422]: auth_request returned 401 — unauthorized access to /api/admin from 10.0.9.1",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN oauth2-proxy[8800]: invalid token: signature verification failed — possible token tampering",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR keycloak[5606]: INVALID_TOKEN: token is not active, client=api-server realm=production",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL api-gateway[8800]: 403 Forbidden — API key revoked for client_id=svc-worker-03",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR vault[5601]: permission denied — policy 'read-secrets' not attached to role ml-worker",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN sshd[2200]: PAM authentication failed for user deploy from 10.0.2.11 — account locked",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR aws-sdk[9910]: AuthorizationTokenExpiredException: security token expired, re-authentication required",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL kubernetes[831]: Unauthorized: cannot list resource 'pods' — ServiceAccount token invalid",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR postgres[2211]: FATAL: password authentication failed for user 'app_user' — 3 consecutive failures",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN ldap[5607]: bind failed for cn=svc-account,dc=corp — invalid credentials, account may be locked",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR consul[5601]: RPC: permission denied — ACL token missing required policy 'service:write'",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL fastapi[6600]: HTTPException 401 — Bearer token invalid or expired for /api/v1/model/predict",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR git[9900]: remote: HTTP Basic: Access denied — deploy key revoked for repo ml-models",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN fail2ban[2200]: 10.0.5.22 banned — 10 failed authentication attempts in 60s",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR grafana[8800]: Login failed — user admin authentication error: wrong password",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL iam[5608]: assumed role expired — STS token for ml-pipeline-role invalid since 2h ago",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR django[9910]: PermissionDenied: User 4421 lacks 'admin' group for /admin/metrics",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN rabbitmq[3309]: access refused for vhost '/prod' — user 'worker' lacks permissions",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR cert-manager[5609]: certificate validation failed — mTLS client cert expired 2d ago",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL airflow[6614]: authentication failed for user pipeline_runner — LDAP connection error",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR redis[3391]: NOAUTH Authentication required — client connected without password",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN istio[3300]: mTLS handshake failed: certificate CN mismatch — possible mitm attack",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR github-actions[9900]: ghp_token authentication failed — PAT expired or revoked",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL service-mesh[3300]: 401 Unauthorized — service-to-service token invalid, rotation needed",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR mysql[1204]: Access denied for user 'app'@'10.0.1.5' to database 'production'",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN api-server[8800]: rate limit exceeded for API key svc-003 — 429 Too Many Requests",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR node[4411]: UnauthorizedError: jwt malformed — invalid base64 in token header",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL auth-service: repeated authentication failures from 10.0.9.1 — brute force detected",
     "error_category": "Auth_Error", "action_type": "escalate_to_human"},

    # ══════════════════════════════════════════════════════════════════════════
    # 8. Permission_Denied (30개) — 파일/시스템 권한 거부
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR nginx[4422]: open() '/etc/nginx/nginx.conf' failed (13: Permission denied)",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL systemd[1]: Permission denied writing PID file /var/run/app/app.pid (errno=13)",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR python3[9910]: PermissionError: [Errno 13] Permission denied: '/var/data/model.pkl'",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN docker[911]: failed to create /var/lib/docker/volumes — permission denied",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR postgres[2211]: could not open file '/var/lib/postgresql/data': Permission denied",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL apache[4411]: Permission denied: access to /var/www/html/secure denied by server config",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR celery[5501]: PermissionError: cannot write to /var/log/celery/worker.log — owner mismatch",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN kubernetes[831]: forbidden: User 'svc-ml' cannot create resource 'deployments' in namespace 'prod'",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR redis[3391]: Permission denied: failed to create /var/lib/redis/dump.rdb (mode 700)",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL cron[1200]: (root) FAILED to authorize user (permission denied to run /opt/scripts/cleanup.sh)",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR java-app[5503]: FileNotFoundException: /opt/config/app.properties (Permission denied)",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN ansible[9900]: FAILED! permission denied while connecting SSH — user 'ansible' lacks sudo",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR consul[5601]: failed to write snapshot: open /data/consul/raft.db: permission denied",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL nginx[4422]: cannot load certificate /etc/ssl/private/cert.key: Permission denied",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR mount[2200]: mount: permission denied (are you root?) mounting /dev/sdb1",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN github-actions[9900]: Error: EACCES: permission denied, scandir '/home/runner/work'",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR fluentd[7700]: Permission denied @ rb_sysopen — /var/log/app/application.log",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL airflow[6614]: PermissionError: [Errno 13] writing to /opt/airflow/dags — check DAG folder ownership",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR terraform[9900]: AccessDenied — user arn:aws:iam::123:user/ci lacks s3:PutObject permission",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN rsyslog[8800]: file '/var/log/custom.log': open error 13 (Permission denied) — dropping logs",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR node[4411]: Error: EACCES: permission denied, open '/etc/hosts'",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL etcd[5601]: cannot create data directory /var/lib/etcd: permission denied",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR logrotate[1200]: error: skipping '/var/log/nginx/error.log' because parent directory has insecure permissions",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN prometheus[2109]: open /etc/prometheus/prometheus.yml: permission denied — config not reloaded",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR gunicorn[7743]: [Errno 13] Permission denied — cannot bind socket /var/run/gunicorn.sock",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL mlflow[6600]: OSError: [Errno 13] Permission denied: '/mnt/nfs/experiments'",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR mysql[1204]: Can't create/write to file '/tmp/mysql.sock' (Errcode: 13 - Permission denied)",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "WARN filebeat[9900]: harvester permission error reading /var/log/auth.log — check file ACL",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "ERROR containerd[911]: failed to create rootfs snapshot: mkdir /var/lib/containerd: permission denied",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL config-manager[9910]: PermissionError writing /etc/nginx/sites-enabled — manual intervention required",
     "error_category": "Permission_Denied", "action_type": "escalate_to_human"},

    # ══════════════════════════════════════════════════════════════════════════
    # 9. Port_Conflict (30개) — 포트 충돌 및 바인딩 실패
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR nginx[4422]: bind() to 0.0.0.0:80 failed (98: Address already in use)",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL java-app[5503]: java.net.BindException: Address already in use — port 8080",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR redis[3391]: Creating Server TCP listening socket 0.0.0.0:6379: bind: Address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN systemd[1]: api-server.service: Start request repeated too quickly — port 9090 conflict",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR postgres[2211]: could not bind IPv4 address '0.0.0.0': Address already in use — port 5432",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL node[4411]: Error: listen EADDRINUSE: address already in use :::3000",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR apache[4411]: (98)Address already in use: AH00072: make_sock: could not bind to address 0.0.0.0:443",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN prometheus[2109]: listen tcp 0.0.0.0:9090: bind: address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR gunicorn[7743]: [ERROR] Connection in use: ('0.0.0.0', 8000) — port 8000 already bound",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL mongo[4450]: Failed to set up listener: 0.0.0.0:27017: bind: Address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR haproxy[2211]: [ALERT] Starting frontend http: cannot bind socket 0.0.0.0:80",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN flask[9910]: OSError: [Errno 98] Address already in use — port 5000 conflict detected",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR grafana[8800]: http: listen tcp :3000: bind: address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL elasticsearch[5501]: publish address 0.0.0.0:9200 already in use — binding failed",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR rabbitmq[3309]: could not bind AMQP socket 5672 — EADDRINUSE from prior instance",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN docker[911]: failed to start container — port 8443:8443 already allocated",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR php-fpm[4411]: unable to bind listening socket for address 'tcp://0.0.0.0:9000': address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL fastapi[6600]: uvicorn: ERROR: [Errno 98] error while attempting to bind on address 0.0.0.0:8000",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR consul[5601]: error starting agent: bind: address already in use for 8500",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN kafka[5501]: Failed to open log segment on port 9092: Address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR traefik[2211]: Error creating server: listen tcp :80 bind: address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL etcd[5601]: Failed to bind listener on 0.0.0.0:2380: EADDRINUSE",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR celery[5501]: flower: error — port 5555 already in use (previous instance not cleaned up)",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN envoy[3300]: failed to bind listener address 0.0.0.0:10000 — address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR zookeeper[3310]: JMX: Already listening on 0.0.0.0:2181 — port conflict",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL airflow[6614]: webserver: OSError address already in use port 8080 — zombie process",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR mlflow[6600]: OSError: [Errno 98] Address already in use — mlflow UI port 5000",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "WARN jenkins[8801]: Failed to listen on TCP port 50000: address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "ERROR keycloak[5606]: Failed to bind port 8080: Address already in use",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},
    {"log_text": "CRITICAL api-server: bind failed on 0.0.0.0:8080 — another instance still running (check zombie)",
     "error_category": "Port_Conflict", "action_type": "execute_rule_command", "command": "ss -tuln"},

    # ══════════════════════════════════════════════════════════════════════════
    # 10. Configuration_Error (30개) — 설정 파일 오류/환경변수 누락
    # ══════════════════════════════════════════════════════════════════════════
    {"log_text": "ERROR nginx[4422]: [emerg] unknown directive 'proxy_cache_methods' in /etc/nginx/nginx.conf:42",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL django[9910]: ImproperlyConfigured: DATABASES 'default' not configured — SECRET_KEY missing",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR mysql[1204]: [ERROR] Fatal error: Can't open and lock privilege tables: Table 'mysql.user' doesn't exist",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN k8s[831]: ConfigMap 'app-config' missing key DATABASE_URL — pod env var substitution failed",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR prometheus[2109]: error loading config file /etc/prometheus/prometheus.yml: unknown fields",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL kafka[5501]: kafka.common.KafkaException: Error configuring broker: invalid log.dirs value",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR python3[9910]: KeyError: 'OPENAI_API_KEY' — required environment variable not set",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN apache[4411]: Invalid command 'SSLEngine' in /etc/apache2/sites-enabled/default-ssl.conf — mod_ssl not loaded",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR fastapi[6600]: pydantic.error_wrappers.ValidationError — REDIS_URL must be a valid URL (got 'localhost')",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL postgres[2211]: invalid value for parameter 'max_connections': '10000' — exceeds system limit",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR redis[3391]: Invalid argument maxmemory-policy 'unknown-policy' in /etc/redis/redis.conf",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN helm[9900]: chart validation failed: values.yaml missing required field 'image.tag'",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR java-app[5503]: BeanCreationException: application.yml property 'spring.datasource.url' is null",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL terraform[9900]: Error: Invalid provider configuration — AWS_REGION environment variable not set",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR haproxy[2211]: [ALERT] parsing [/etc/haproxy/haproxy.cfg]: invalid argument in 'backend' section",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN gunicorn[7743]: invalid configuration: 'workers' (12) exceeds recommended maximum for 2 CPUs",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR docker-compose[911]: ERROR: Cannot locate specified Dockerfile: 'Dockerfile.prod' not found",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL airflow[6614]: AirflowConfigException: AIRFLOW__CORE__FERNET_KEY is not set",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR elasticsearch[5501]: unable to load configuration: unknown setting [indices.memory.unknown_setting]",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN celery[5501]: configuration error: CELERY_BROKER_URL not specified — using default amqp://",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR traefik[2211]: error parsing configuration file: yaml: line 34: did not find expected key",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL vault[5601]: configuration error: listener 'tcp': address not specified",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR istio[3300]: error reading mesh config: unable to parse MeshConfig proto — invalid field 'unknownPolicy'",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN fluentd[7700]: config error: plugin 'elasticsearch' output host not configured",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR logstash[7700]: Failed to execute action {:action=>LogStash::PipelineAction::Create} — bad config",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL grafana[8800]: Failed to parse config file /etc/grafana/grafana.ini: unknown property 'auth.proxy.enabled'",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR spark[3001]: SparkException: python in worker has different version 3.8 than driver Python 3.10",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "WARN ansible[9900]: ERROR! the role 'common' was not found in /etc/ansible/roles — check roles_path",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "ERROR certbot[5609]: Error: The certificate renewal configuration file /etc/letsencrypt/renewal/domain.conf is broken",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
    {"log_text": "CRITICAL systemd[1]: api-server.service configuration error — ExecStart path not absolute",
     "error_category": "Configuration_Error", "action_type": "escalate_to_human"},
]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def get_or_create_collection(client):
    try:
        return client.get_collection(COLLECTION_NAME)
    except Exception:
        return client.create_collection(
            COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )


def load_entries(col):
    ids, docs, metas = [], [], []
    for entry in SYSLOG_ENTRIES:
        uid = ID_PREFIX + _md5(entry["log_text"])
        ids.append(uid)
        docs.append(entry["log_text"])
        meta = {
            "error_category": entry["error_category"],
            "action_type":    entry["action_type"],
            "target_process": entry.get("target_process", ""),
            "command":        entry.get("command", ""),
            "source":         "syslog_augment_v1",
        }
        metas.append(meta)

    batch = 100
    added = 0
    for i in range(0, len(ids), batch):
        col.upsert(
            ids=ids[i:i+batch],
            documents=docs[i:i+batch],
            metadatas=metas[i:i+batch],
        )
        added += len(ids[i:i+batch])
        console.print(f"  적재 {added}/{len(ids)}...", end="\r")
    return len(ids)


def remove_entries(col):
    data = col.get(include=["metadatas"])
    rm_ids = [
        data["ids"][i]
        for i, m in enumerate(data["metadatas"])
        if m.get("source") == "syslog_augment_v1"
    ]
    if rm_ids:
        col.delete(ids=rm_ids)
    return len(rm_ids)


def verify(col):
    table = Table(title="카테고리별 top-1 매칭 검증", box=box.SIMPLE_HEAVY)
    table.add_column("카테고리", style="cyan")
    table.add_column("distance", justify="right")
    table.add_column("action_type")
    table.add_column("L1 HIT?", justify="center")

    probes = {
        "Out_Of_Memory":      "kernel: Out of memory Kill process api-server OOM",
        "Memory_Leak":        "worker heap growing memory leak RSS unbounded intervention",
        "Disk_Full":          "CRITICAL disk full /dev/sda1 No space left on device write failed",
        "Process_Crash":      "nginx worker exited with signal 11 SIGSEGV service unavailable",
        "Network_Timeout":    "Connection timeout upstream unreachable timed out 30000ms",
        "DB_Connection":      "Database connection pool exhausted postgres connections refused",
        "Auth_Error":         "Authentication failed invalid credentials repeated failure brute force",
        "Permission_Denied":  "PermissionError EACCES permission denied cannot write config file",
        "Port_Conflict":      "bind failed EADDRINUSE address already in use port 8080",
        "Configuration_Error":"invalid configuration environment variable not set missing required",
    }

    all_ok = True
    for cat, probe in probes.items():
        r = col.query(query_texts=[probe], n_results=1)
        d = r["distances"][0][0]
        m = r["metadatas"][0][0]
        hit = d < 1.2
        if not hit:
            all_ok = False
        table.add_row(
            cat,
            f"{d:.3f}",
            m.get("action_type", "?"),
            "✅" if hit else "❌",
        )

    console.print(table)
    return all_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="검색 결과만 확인")
    parser.add_argument("--remove", action="store_true", help="추가한 항목 삭제")
    args = parser.parse_args()

    client = chromadb.PersistentClient(
        path=CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )
    col = get_or_create_collection(client)
    before = col.count()

    if args.remove:
        n = remove_entries(col)
        rprint(f"[red]삭제 완료: {n}개 제거 (현재 {col.count()}개)[/]")
        return

    if args.verify:
        verify(col)
        return

    console.print(f"\n[bold cyan]syslog 학습 데이터 적재 시작[/] (현재 {before}개)")
    n = load_entries(col)
    after = col.count()
    console.print(f"\n[bold green]완료: {n}개 upsert → 총 {after}개 벡터[/]")
    console.print()
    verify(col)


if __name__ == "__main__":
    main()
