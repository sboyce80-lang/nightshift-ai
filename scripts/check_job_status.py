"""Quick status check for a single submission by full or prefix ID.

Usage:
    python3 scripts/check_job_status.py 45c7eca6           # prefix match
    python3 scripts/check_job_status.py 45c7eca6-...-uuid  # full UUID

Requires the same env vars as the other prod scripts:
    DATABASE_URL, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET

Prints:
  - status / version / submitted_at / updated_at / error (if any)
  - upload file count + names
  - result file count + names + R2 keys
  - elapsed time since submit + since last update
  - if status == "queued" or "processing", how long it's been in that state
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db import engine
from models import File, Submission


def _fmt_age(dt) -> str:
    if not dt:
        return "—"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h ago"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    job_id = sys.argv[1].strip()
    for var in ("DATABASE_URL", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        if not os.environ.get(var):
            print(f"FATAL: {var} not set", file=sys.stderr)
            return 2

    with Session(engine) as s:
        q = sa.select(Submission)
        if len(job_id) < 36:
            # Prefix match
            q = q.where(Submission.id.like(f"{job_id}%"))
        else:
            q = q.where(Submission.id == job_id)
        subs = s.execute(q.order_by(Submission.submitted_at.desc())).scalars().all()

        if not subs:
            print(f"No submission found matching {job_id!r}", file=sys.stderr)
            return 1
        if len(subs) > 1:
            print(f"Multiple submissions match prefix {job_id!r}:")
            for sub in subs:
                print(f"  {sub.id}  status={sub.status}  "
                      f"submitted={_fmt_age(sub.submitted_at)}")
            print()
            print("Pass the full UUID to disambiguate.", file=sys.stderr)
            return 1

        sub = subs[0]

        files = s.execute(
            sa.select(File).where(File.submission_id == sub.id)
            .order_by(File.created_at.asc())
        ).scalars().all()

        uploads = [f for f in files if f.kind == "upload"]
        results = [f for f in files if f.kind == "result"]
        other = [f for f in files if f.kind not in ("upload", "result")]

    # Print report
    print(f"Submission {sub.id}")
    print(f"  Status:        {sub.status}")
    print(f"  Version:       {sub.version}")
    print(f"  Business:      {sub.business_name or '(none)'}")
    print(f"  Submitted:     {sub.submitted_at}  ({_fmt_age(sub.submitted_at)})")
    print(f"  Updated:       {sub.updated_at}  ({_fmt_age(sub.updated_at)})")
    if sub.error:
        print(f"  Error:         {sub.error}")
    if sub.subtotal is not None:
        print(f"  Subtotal:      ${sub.subtotal:,.2f}")
    print()
    print(f"  Uploads ({len(uploads)}):")
    for f in uploads:
        print(f"    {f.filename}  ({f.r2_key})")
    print(f"  Results ({len(results)}):")
    for f in results:
        print(f"    {f.filename}  ({f.r2_key})  created {_fmt_age(f.created_at)}")
    if other:
        print(f"  Other ({len(other)}):")
        for f in other:
            print(f"    kind={f.kind}  {f.filename}")

    print()
    if sub.status in ("queued", "processing", "running"):
        print(f"⏳ Job has been in '{sub.status}' state for "
              f"{_fmt_age(sub.updated_at)}.")
    elif sub.status == "completed":
        print(f"✅ Completed.")
    elif sub.status in ("failed", "error"):
        print(f"❌ Failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
