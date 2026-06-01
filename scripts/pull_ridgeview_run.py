"""One-off: pull Elliott's Ridgeview submission JSON from prod (Postgres + R2).

Requires these env vars set in the shell before running:
    DATABASE_URL
    R2_ACCOUNT_ID
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET

Writes the result JSON to output/ridgeview_prod_<submission_id>.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db import get_engine
from models import File, Submission
from storage import download_file


def main() -> int:
    for var in ("DATABASE_URL", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        if not os.environ.get(var):
            print(f"FATAL: {var} not set", file=sys.stderr)
            return 2

    engine = get_engine()
    with Session(engine) as s:
        # Match by upload filename — Elliott uploaded the Ridgeview PDF;
        # whatever its exact filename, it should contain "ridgeview".
        rows = s.execute(
            sa.select(Submission, File)
            .join(File, File.submission_id == Submission.id)
            .where(File.kind == "upload")
            .where(sa.func.lower(File.filename).like("%ridgeview%"))
            .order_by(Submission.submitted_at.desc())
            .limit(10)
        ).all()

        if not rows:
            print("No submissions found with 'ridgeview' in upload filename.",
                  file=sys.stderr)
            print("Listing 10 most recent submissions for reference:",
                  file=sys.stderr)
            recent = s.execute(
                sa.select(Submission, sa.func.string_agg(File.filename, ", "))
                .join(File, File.submission_id == Submission.id)
                .where(File.kind == "upload")
                .group_by(Submission.id)
                .order_by(Submission.submitted_at.desc())
                .limit(10)
            ).all()
            for sub, names in recent:
                print(f"  {sub.submitted_at}  {sub.id}  {sub.status}  {names}",
                      file=sys.stderr)
            return 1

        print("Matching submissions (most recent first):")
        for sub, f in rows:
            print(f"  {sub.submitted_at}  id={sub.id}  status={sub.status}  "
                  f"v={sub.version}  upload={f.filename}")

        target_sub_id = rows[0][0].id
        print(f"\nPulling result files for submission {target_sub_id} ...")

        results = s.execute(
            sa.select(File)
            .where(File.submission_id == target_sub_id)
            .where(File.kind == "result")
            .order_by(File.created_at.desc())
        ).scalars().all()

        if not results:
            print("No result files attached to this submission "
                  "(may still be queued/failed).", file=sys.stderr)
            return 1

        out_dir = REPO / "output"
        out_dir.mkdir(exist_ok=True)
        for f in results:
            local = out_dir / f"ridgeview_prod_{target_sub_id}_{f.filename}"
            print(f"  {f.r2_key}  →  {local}")
            download_file(f.r2_key, str(local))

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
