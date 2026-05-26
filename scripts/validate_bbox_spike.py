#!/usr/bin/env python3
"""
Validate the Tier-1 bbox spike against a (PDF, result.json) pair.

Outputs:
    1. Coverage report to stdout (overall + per-page + per-match-quality)
    2. Annotated PDF: every source_page rendered with room bboxes + labels drawn
       on top, every non-source page passed through untouched. The whole
       multi-sheet drawing set becomes a single "verification deck."

Usage:
    python3 scripts/validate_bbox_spike.py \\
        --pdf spike_samples/364Main.pdf \\
        --result spike_samples/364Main.result.json \\
        --out spike_samples/364Main.annotated.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running from anywhere — bbox_spike lives at repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bbox_spike import attach_label_bboxes, render_annotated_pdf


def _print_report(result: dict, pdf_path: str) -> None:
    # Summary is written where "floors" lives: analysis.bbox_spike_summary for
    # wrapped results, or top-level for raw analysis dicts.
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else result
    s = analysis.get("bbox_spike_summary") or result.get("bbox_spike_summary") or {}
    total = s.get("total_rooms", 0)
    anchored = s.get("anchored", 0)
    by_q = s.get("by_quality", {})
    per_page = s.get("per_page", {})

    print(f"\n=== Tier-1 bbox spike coverage ===")
    print(f"PDF:       {pdf_path}")
    print(f"Rooms:     {total}")
    print(f"Anchored:  {anchored} ({s.get('coverage_pct', 0)}%)")
    print(f"By quality: exact={by_q.get('exact',0)}  "
          f"ci={by_q.get('ci',0)}  "
          f"normalized={by_q.get('normalized',0)}  "
          f"token={by_q.get('token',0)}  "
          f"MISS={by_q.get('miss',0)}  "
          f"no_page={by_q.get('no_page',0)}")

    print(f"\nPer-page coverage:")
    for pg in sorted(per_page, key=lambda k: int(k) if str(k).isdigit() else 0):
        p = per_page[pg]
        pct = round(100.0 * p["hits"] / p["total"], 1) if p["total"] else 0.0
        q_str = " ".join(f"{k}={v}" for k, v in sorted(p["qualities"].items()))
        print(f"  p{pg:>2}: {p['hits']:>3}/{p['total']:<3} ({pct:>5.1f}%)   [{q_str}]")

    # Miss + ambiguous detail (helps decide if Tier 2 is worth doing)
    misses = []
    ambiguous = []
    for floor in (result.get("analysis") or {}).get("floors", []):
        for r in floor.get("rooms", []):
            b = r.get("bbox") or {}
            if b.get("match_quality") is None:
                misses.append((floor.get("floor_name", "?"), r.get("room_name", "?"),
                               r.get("source_page", "?")))
            elif (b.get("candidates_on_page") or 0) > 1:
                ambiguous.append((floor.get("floor_name", "?"), r.get("room_name", "?"),
                                  r.get("source_page", "?"), b["match_quality"],
                                  b["candidates_on_page"]))

    if misses:
        print(f"\nMisses ({len(misses)}):")
        for floor, name, sp in misses:
            print(f"  [{floor}] {name!r}  (p{sp})")
    if ambiguous:
        print(f"\nAmbiguous matches (multiple label candidates on page; first chosen):")
        for floor, name, sp, q, n in ambiguous[:15]:
            print(f"  [{floor}] {name!r}  (p{sp}, {q}, {n} candidates)")
        if len(ambiguous) > 15:
            print(f"  ... and {len(ambiguous) - 15} more")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="Source architectural PDF")
    ap.add_argument("--result", required=True, help="Takeoff result JSON")
    ap.add_argument("--out", required=True, help="Annotated PDF output path")
    ap.add_argument("--write-json", help="Optional: write augmented JSON here too")
    args = ap.parse_args()

    with open(args.result) as f:
        result = json.load(f)

    result = attach_label_bboxes(result, args.pdf)
    _print_report(result, args.pdf)
    render_annotated_pdf(args.pdf, result, args.out)

    print(f"\nWrote annotated PDF → {args.out}")
    if args.write_json:
        with open(args.write_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote augmented JSON → {args.write_json}")


if __name__ == "__main__":
    main()
