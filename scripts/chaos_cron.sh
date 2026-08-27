#!/usr/bin/env bash
# 카오스 엔지니어링 주기 실행기 — cron에서 호출.
# CHAOS_ENABLED=false(.env)면 즉시 종료하는 킬 스위치가 있고,
# flock으로 한 번에 하나의 주입만 실행되도록 보장한다.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
LOCK_FILE="/tmp/chaos_injector.lock"
AUDIT_LOG="$REPO_DIR/data/chaos_injector.log"
TARGET_URL="http://localhost:9000"
FAULT_TYPES=(oom cpu diskfull process_crash)

if [ -f "$ENV_FILE" ] && ! grep -qE '^CHAOS_ENABLED=true\b' "$ENV_FILE"; then
    exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) SKIP another chaos injection already in progress" >> "$AUDIT_LOG"
    exit 0
fi

fault="${FAULT_TYPES[$RANDOM % ${#FAULT_TYPES[@]}]}"

http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$TARGET_URL/inject/$fault")
curl_status=$?

# process_crash는 SIGKILL로 연결이 끊기므로 curl이 비정상 종료해도 정상 동작임
if [ "$fault" = "process_crash" ] && [ "$curl_status" -ne 0 ]; then
    echo "$(date -u +%FT%TZ) OK fault=$fault (connection reset expected — process crashed as intended)" >> "$AUDIT_LOG"
elif [ "$curl_status" -eq 0 ] && [ "$http_code" = "200" ]; then
    echo "$(date -u +%FT%TZ) OK fault=$fault http=$http_code" >> "$AUDIT_LOG"
else
    echo "$(date -u +%FT%TZ) FAIL fault=$fault http=$http_code curl_status=$curl_status" >> "$AUDIT_LOG"
fi
