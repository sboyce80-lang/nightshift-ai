#!/usr/bin/env python3
"""Print the VME promotion verdict from each submission's LATEST result JSON.

Ops tool for the 2026-07-08 VME flip: after a re-run, confirm from the stored
result whether the geometric measurement was promoted (`_vme_authoritative` /
`_vme_primary`) or abstained (and why), without paging through worker logs.

Usage (on a Render worker via `render jobs create`, needs DATABASE_URL + R2_*):

    python scripts/print_vme_verdicts.py <id8-or-uuid> [<id8-or-uuid> ...]
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

import storage  # noqa: E402


def main():
    ids = sys.argv[1:]
    if not ids:
        print("usage: print_vme_verdicts.py <submission-id> ...")
        return 2

    db_url = os.environ["DATABASE_URL"]
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    engine = create_engine(db_url)

    for sid in ids:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT f.submission_id, f.r2_key, f.filename "
                "FROM files f "
                "WHERE f.submission_id LIKE :p AND f.kind = 'result' "
                "AND f.filename LIKE '%.json' "
                "ORDER BY f.created_at DESC LIMIT 1"
            ), {"p": sid + "%"}).first()
        if not row:
            print(f"{sid}: no result JSON")
            continue
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "r.json")
            storage.download_file(row.r2_key, path)
            with open(path) as f:
                result = json.load(f)
        analysis = result.get("analysis", result)
        agg = analysis.get("aggregated_totals") or {}
        auth = analysis.get("_vme_authoritative")
        prim = analysis.get("_vme_primary")
        shadow = analysis.get("_vme_shadow_v2") or {}
        print(f"=== {row.submission_id[:8]}  ({row.filename})")
        print(f"  walls priced:   {agg.get('total_paintable_wall_sqft')}")
        print(f"  shadow_v2:      lf={shadow.get('total_wall_run_lf')} "
              f"est_sf={shadow.get('est_wall_sf')} "
              f"unmeasured={len(shadow.get('unmeasured') or [])}")
        print(f"  authoritative:  {json.dumps(auth)[:300]}")
        print(f"  primary:        {json.dumps(prim)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
