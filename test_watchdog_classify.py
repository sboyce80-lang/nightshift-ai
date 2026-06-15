#!/usr/bin/env python3
"""Offline tests for the v2 stuck-job watchdog decision core.

Pins the rule that distinguishes v2 from the never-deployed v1: a job is
reaped only when BOTH liveness signals are dead. v1 swept on updated_at
age alone and would have killed healthy 90-minute takeoffs at minute 31,
emailed "please resubmit", and then had the worker flip the row back to
completed (the 2026-06 review's worst-case trust scenario, finding 6.1).

Run: python3 test_watchdog_classify.py
"""
import os
import sys

# sweep_stuck_jobs imports db.engine at module load; give it a harmless
# in-memory engine so the pure decision core can be imported offline.
os.environ.setdefault("DATABASE_URL", "sqlite://")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))

from sweep_stuck_jobs import classify_stuck  # noqa: E402

PASS = FAIL = 0


def check(name, got, want_action):
    global PASS, FAIL
    action, reason = got
    if action == want_action:
        PASS += 1
        print(f"  PASS  {name}  ({reason})")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  got={action} want={want_action} ({reason})")


def main():
    MIN = 60

    # --- The v1 false-positive: healthy long job must be LEFT ALONE ---
    check("healthy 90-min job, RQ started, fresh heartbeat",
          classify_stuck("processing", 30, 90 * MIN, "started"), "leave")
    check("healthy job, RQ started, heartbeat stale (thread died) — never kill while RQ active",
          classify_stuck("processing", 45 * MIN, 90 * MIN, "started"), "leave")
    check("legacy row (no heartbeat), RQ started",
          classify_stuck("processing", None, 5 * 3600, "started"), "leave")

    # --- The real zombie: OOM-killed horse ---
    check("OOM zombie: heartbeat stale, RQ failed",
          classify_stuck("processing", 30 * MIN, 40 * MIN, "failed"), "reap")
    check("OOM zombie: heartbeat stale, RQ missing",
          classify_stuck("processing", 30 * MIN, 40 * MIN, "missing"), "reap")
    check("completion race: RQ finished but heartbeat fresh — recheck later",
          classify_stuck("processing", 60, 40 * MIN, "finished"), "leave")
    check("stale row, RQ finished, heartbeat stale (DB write lost)",
          classify_stuck("processing", 30 * MIN, 40 * MIN, "finished"), "reap")

    # --- Legacy rows (pre-migration, heartbeat NULL) ---
    check("legacy zombie: no heartbeat, RQ missing, 5h old",
          classify_stuck("processing", None, 5 * 3600, "missing"), "reap")
    check("legacy young: no heartbeat, RQ missing, 1h old — within grace",
          classify_stuck("processing", None, 3600, "missing"), "leave")

    # --- Queued rows ---
    check("queued behind a long job, RQ queued",
          classify_stuck("queued", None, 2 * 3600, "queued"), "leave")
    check("lost enqueue: queued 45min, RQ missing",
          classify_stuck("queued", None, 45 * MIN, "missing"), "reap")
    check("queued 10min, RQ missing — within grace",
          classify_stuck("queued", None, 10 * MIN, "missing"), "leave")

    # --- 'running' is treated like processing ---
    check("running + RQ started",
          classify_stuck("running", 30, 60 * MIN, "started"), "leave")
    check("running zombie",
          classify_stuck("running", 30 * MIN, 60 * MIN, "missing"), "reap")

    # --- Unknown RQ state (Redis hiccup) is NOT active -> conservative paths
    check("unknown RQ + fresh heartbeat",
          classify_stuck("processing", 30, 60 * MIN, "unknown"), "leave")

    # --- Terminal-ish states never swept ---
    check("needs_review untouched",
          classify_stuck("needs_review", None, 9 * 3600, "missing"), "leave")

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
