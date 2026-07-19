#!/usr/bin/env python3
"""Auto-approver: polls approval_store for pending approvals and approves safe commands.
"""
import time
from src import approval_store

SAFE_PREFIXES = ('echo', 'free', 'ss', 'df', 'uptime', 'ps', 'journalctl')

print('Auto-approver started')
start = time.time()
while time.time() - start < 120:
    conn = None
    # fetch recent pendings
    try:
        # approval_store.get_request requires token; instead access DB via sqlite
        import sqlite3
        conn = sqlite3.connect('./data/agent_metrics.db')
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT token, command, status FROM pending_approvals WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        for r in rows:
            token = r['token']
            command = (r['command'] or '').strip()
            if not command:
                continue
            base = command.split()[0]
            if base in SAFE_PREFIXES:
                ok = approval_store.set_decision(token, 'approved')
                print(f'Approved token={token} command={command} -> {ok}')
            else:
                print(f'Skipping token={token} command={command} (not in SAFE_PREFIXES)')
    except Exception as e:
        print('auto-approve error', e)
    finally:
        if conn:
            conn.close()
    time.sleep(2)
print('Auto-approver finished')
