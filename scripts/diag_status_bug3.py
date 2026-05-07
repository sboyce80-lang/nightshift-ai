#!/usr/bin/env python3
"""Verify the new raw-UPDATE update_status against prod DB.

Cycles 44ed1e05 through queued -> processing -> failed -> cancelled and
prints the rowcount log line each time. Confirms the new implementation
is observable.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from db import session_scope
from models import Submission
from jobs import update_status   # new raw-UPDATE version

SID = "44ed1e05-cbb8-4352-898e-c63d716d94a2"

def show(label):
    with session_scope() as s:
        sub = s.get(Submission, SID)
        if sub:
            print(f"  [{label}] status={sub.status!r}, updated_at={sub.updated_at}, "
                  f"subtotal={sub.subtotal}")
        else:
            print(f"  [{label}] NOT FOUND")

show("baseline")

print("\n>>> update_status(sid, 'queued')")
update_status(SID, "queued")
show("after queued")

print("\n>>> update_status(sid, 'processing')")
update_status(SID, "processing")
show("after processing")

print("\n>>> update_status(sid, 'failed', error='diag test')")
update_status(SID, "failed", error="diag test")
show("after failed")

print("\n>>> update_status('00000000-aaaa-bbbb-cccc-000000000000', 'processing')   <-- nonexistent")
update_status("00000000-aaaa-bbbb-cccc-000000000000", "processing")

print("\n>>> reset to cancelled for safety")
update_status(SID, "cancelled", error="Cancelled by admin (Steve test job, exposed reenqueue script bug)")
show("final")
