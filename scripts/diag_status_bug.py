#!/usr/bin/env python3
"""Reproduce the status-update bug.

Theory: when reenqueue_stuck.py held a Postgres tx open across the loop,
the worker's update_status('processing') blocked on the row lock, and on
unblock something silently dropped the change. This script reproduces that
condition with two parallel SQLAlchemy sessions and observes the outcome.

Safe to run: only touches the cancelled test row 44ed1e05.
"""
import os, sys, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import session_scope, engine
from models import Submission

SID = "44ed1e05-cbb8-4352-898e-c63d716d94a2"

print(f"Engine URL: {str(engine.url).split('@')[-1]}")
print(f"Pool size: {engine.pool.size()}, overflow: {engine.pool.overflow()}\n")


def worker_update():
    """Mimic jobs.py:update_status — same code path."""
    print(f"[worker] T+{time.time()-T0:.2f}s  calling update_status('processing')")
    try:
        with session_scope() as session:
            sub = session.get(Submission, SID)
            print(f"[worker] T+{time.time()-T0:.2f}s  session.get returned status={sub.status!r}")
            sub.status = "processing"
            print(f"[worker] T+{time.time()-T0:.2f}s  set sub.status='processing', awaiting commit on context exit")
        print(f"[worker] T+{time.time()-T0:.2f}s  session_scope exited cleanly (commit done)")
    except Exception as exc:
        print(f"[worker] T+{time.time()-T0:.2f}s  EXCEPTION: {type(exc).__name__}: {exc}")


# Reset row to a known state
print("Resetting 44ed1e05 to status='cancelled' baseline...")
with session_scope() as s:
    sub = s.get(Submission, SID)
    sub.status = "cancelled"
    sub.error = "diag baseline"

with session_scope() as s:
    sub = s.get(Submission, SID)
    print(f"Baseline: status={sub.status!r}, updated_at={sub.updated_at}\n")

# Now reproduce: hold a tx open with the row locked, while another thread
# tries update_status('processing'). This mimics the reenqueue script
# holding 'queued' for sid#1 while the worker dequeues + tries 'processing'.
T0 = time.time()
print("=== Reproducing lock contention ===")
print(f"[main] T+0.00s  opening session, UPDATE row to 'queued', NOT committing yet")

main_session = engine.connect()
main_tx = main_session.begin()
main_session.execute(
    Submission.__table__.update()
    .where(Submission.id == SID)
    .values(status="queued", error="diag holds lock")
)
print(f"[main] T+{time.time()-T0:.2f}s  UPDATE issued (lock held), sleeping 3s before commit")

t = threading.Thread(target=worker_update)
t.start()
time.sleep(3.0)

print(f"[main] T+{time.time()-T0:.2f}s  committing main tx (releases lock)")
main_tx.commit()
main_session.close()

t.join(timeout=10)

# Final state
with session_scope() as s:
    sub = s.get(Submission, SID)
    print(f"\nFinal: status={sub.status!r}, updated_at={sub.updated_at}, error={sub.error!r}")

if sub.status == "processing":
    print("\n=> Worker UPDATE landed correctly. Theory disproven for plain lock contention.")
else:
    print("\n=> Worker UPDATE was DROPPED. Status stuck at:", sub.status)
