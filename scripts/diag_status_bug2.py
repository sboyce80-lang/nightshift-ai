#!/usr/bin/env python3
"""Test #2: simulate the actual sequence of the real failure.

Real timeline for 44ed1e05:
  T0:   reenqueue script UPDATE row to 'queued', then immediately commits
  T0+ms: script enqueues RQ job in Redis
  T0+~6s: worker dequeues, calls update_status('processing')

Key difference vs Test #1: my script's tx had already committed and
released locks BEFORE the worker tried its UPDATE.

Also: this test uses subprocess.run for the "worker" call to avoid
sharing any in-process state — closest analog to the real fork+exec.
"""
import os, sys, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import session_scope, engine
from models import Submission

SID = "44ed1e05-cbb8-4352-898e-c63d716d94a2"

# Worker subprocess — does exactly what jobs.py:update_status does
WORKER_SCRIPT = """
import os, sys
sys.path.insert(0, "{repo}")
from db import session_scope
from models import Submission

with session_scope() as session:
    sub = session.get(Submission, "{sid}")
    print(f"[worker-subprocess] loaded status={{sub.status!r}}", flush=True)
    sub.status = "processing"
    print(f"[worker-subprocess] set status='processing', committing on context exit", flush=True)
print("[worker-subprocess] commit done")

with session_scope() as session:
    sub = session.get(Submission, "{sid}")
    print(f"[worker-subprocess] re-read status={{sub.status!r}}")
""".format(repo=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), sid=SID)


def show_state(label):
    with session_scope() as s:
        sub = s.get(Submission, SID)
        print(f"  [{label}] status={sub.status!r}, updated_at={sub.updated_at}")


# Reset baseline
print("Resetting baseline to status='cancelled'...")
with session_scope() as s:
    sub = s.get(Submission, SID)
    sub.status = "cancelled"
    sub.error = "diag2 baseline"
show_state("baseline")

# Step 1: simulate reenqueue script's UPDATE+commit
print("\nSTEP 1: 'reenqueue script' sets status='queued' and commits")
with session_scope() as s:
    sub = s.get(Submission, SID)
    sub.status = "queued"
    sub.error = None
show_state("after script commit")

# Step 2: tiny gap (~50ms) like the real RQ enqueue + dequeue latency
time.sleep(0.05)

# Step 3: spawn worker subprocess to call update_status('processing')
print("\nSTEP 2: spawn worker subprocess to call update_status('processing')")
result = subprocess.run(
    [sys.executable, "-c", WORKER_SCRIPT],
    env={**os.environ},
    capture_output=True, text=True, timeout=30,
)
print("  stdout:")
for line in result.stdout.splitlines():
    print("   ", line)
if result.stderr.strip():
    print("  stderr:")
    for line in result.stderr.splitlines():
        print("   ", line)
print("  returncode:", result.returncode)

# Step 4: from a fresh session, observe final state
print("\nSTEP 3: read final state from a fresh main-process session")
show_state("final")
