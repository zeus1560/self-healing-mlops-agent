from fastapi import FastAPI, BackgroundTasks
import threading, time, os, logging
from datetime import datetime

app = FastAPI(title="Target App (Simulated)")
log_path = "/app/data/target_app.log" if os.path.isdir('/app/data') else "./data/target_app.log"

os.makedirs(os.path.dirname(log_path), exist_ok=True)

# background writer to simulate normal traffic
_stop_event = threading.Event()

def _writer():
    i = 0
    while not _stop_event.is_set():
        with open(log_path, "a", encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()} INFO api: request handled id={i}\n")
        i += 1
        time.sleep(2)

threading.Thread(target=_writer, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/inject/oom")
async def inject_oom(background_tasks: BackgroundTasks):
    """Write lines resembling an OOM kernel message to the log to simulate OOM detection."""
    def task():
        with open(log_path, "a", encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()} ERROR kernel: Out of memory: Kill process 1234 (api-server) score 999\n")
    background_tasks.add_task(task)
    return {"injected": "oom"}

@app.post("/inject/diskfull")
async def inject_diskfull(background_tasks: BackgroundTasks):
    def task():
        with open(log_path, "a", encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()} CRITICAL systemd: disk full — /dev/sda1 at 100% capacity\n")
    background_tasks.add_task(task)
    return {"injected": "diskfull"}

@app.post("/inject/process_crash")
async def inject_process_crash(background_tasks: BackgroundTasks):
    def task():
        with open(log_path, "a", encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()} ERROR api-server: FATAL — process crashed with signal 11\n")
    background_tasks.add_task(task)
    return {"injected": "process_crash"}

@app.post("/stop")
async def stop_app():
    _stop_event.set()
    return {"stopped": True}
