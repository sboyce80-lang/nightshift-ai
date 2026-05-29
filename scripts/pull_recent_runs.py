"""Bulk-download recent completed-submission result JSONs from R2 into a
local corpus directory for offline regression testing.

Companion to scripts/regression_corpus.py — that script consumes whatever
this script puts in output/regression_corpus/ and runs the dedup +
reference-case checks. Splitting download from evaluation lets us iterate
on the evaluation logic without re-hitting R2 every time.

Required env vars (same as scripts/pull_ridgeview_run.py):
    DATABASE_URL
    R2_ACCOUNT_ID
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET

Usage:
    python3 scripts/pull_recent_runs.py                      # last 50 jobs
    python3 scripts/pull_recent_runs.py --limit 200          # last 200
    python3 scripts/pull_recent_runs.py --since 2026-05-01   # date filter
    python3 scripts/pull_recent_runs.py --status completed   # default
    python3 scripts/pull_recent_runs.py --out output/corpus  # custom dir

Idempotent — skips submissions whose result JSON is already on disk
(checked by submission_id). Safe to re-run after a new batch of jobs
lands in prod.

Writes:
    <out_dir>/<submission_id>.json         — the result JSON
    <out_dir>/manifest.csv                 — submission_id, contact_email,
                                              document, submitted_at, version
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db import engine
from models import File, Submission
from storage import download_file


def _check_env() -> None:
    missing = [v for v in (
        "DATABASE_URL", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
    ) if not os.environ.get(v)]
    if missing:
        print(f"FATAL: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limit", type=int, default=50,
                   help="Max submissions to download (default 50)")
    p.add_argument("--since", default=None,
                   help="Only submissions submitted on or after this date "
                        "(YYYY-MM-DD)")
    p.add_argument("--status", default="completed",
                   help="Submission status filter (default 'completed'; "
                        "use 'any' to skip)")
    p.add_argument("--out", default=str(REPO / "output" / "regression_corpus"),
                   help="Output directory (default output/regression_corpus)")
    p.add_argument("--filename-pattern", default="construction_analysis_%.json",
                   help="SQL LIKE pattern for result filenames "
                        "(default construction_analysis_*.json)")
    args = p.parse_args()

    _check_env()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.csv"

    with Session(engine) as s:
        q = sa.select(Submission).order_by(Submission.submitted_at.desc())
        if args.status != "any":
            q = q.where(Submission.status == args.status)
        if args.since:
            try:
                since_dt = datetime.fromisoformat(args.since)
            except ValueError:
                print(f"FATAL: --since must be YYYY-MM-DD; got {args.since!r}",
                      file=sys.stderr)
                return 2
            q = q.where(Submission.submitted_at >= since_dt)
        q = q.limit(args.limit)
        submissions = s.execute(q).scalars().all()

        print(f"Found {len(submissions)} submission(s) matching filters.")
        if not submissions:
            return 0

        # Existing files to skip
        existing = {p.stem for p in out_dir.glob("*.json")}
        new_rows: list[dict] = []
        downloaded = 0
        skipped = 0
        missing = 0

        for sub in submissions:
            sub_id = str(sub.id)
            if sub_id in existing:
                skipped += 1
                continue

            # Locate the construction_analysis JSON result file
            result_file = s.execute(
                sa.select(File)
                .where(File.submission_id == sub.id)
                .where(File.kind == "result")
                .where(sa.func.lower(File.filename).like(args.filename_pattern))
                .order_by(File.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            if not result_file:
                missing += 1
                continue

            # Find the upload file (for document name / contact metadata)
            upload_file = s.execute(
                sa.select(File)
                .where(File.submission_id == sub.id)
                .where(File.kind == "upload")
                .order_by(File.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()

            local = out_dir / f"{sub_id}.json"
            try:
                download_file(result_file.r2_key, str(local))
            except Exception as exc:
                print(f"  ❌ {sub_id}: download failed — {exc}", file=sys.stderr)
                continue

            doc_name = (upload_file.filename if upload_file
                        else result_file.filename)
            new_rows.append({
                "submission_id": sub_id,
                "submitted_at": sub.submitted_at.isoformat()
                                  if sub.submitted_at else "",
                "version": sub.version,
                "status": sub.status,
                "business_name": sub.business_name or "",
                "document": doc_name,
                "result_filename": result_file.filename,
                "r2_key": result_file.r2_key,
                "local_path": str(local.relative_to(REPO)),
            })
            downloaded += 1
            print(f"  ✅ {sub_id}  v{sub.version}  {doc_name}")

        # Append to manifest (create with header if new)
        if new_rows:
            write_header = not manifest_path.exists()
            with open(manifest_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(new_rows[0].keys()))
                if write_header:
                    w.writeheader()
                w.writerows(new_rows)

    print()
    print(f"Downloaded: {downloaded}")
    print(f"Skipped (already on disk): {skipped}")
    print(f"Missing result JSON in R2:  {missing}")
    print(f"Corpus dir: {out_dir}")
    print(f"Manifest:   {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
