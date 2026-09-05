#!/usr/bin/env python3
"""Diagnose room-geometry measurement failures per page, offline.

Replicates _compute_room_geometry_shadow's anchor construction from a
stored roster, then reruns measure_room_areas per page with
instrumentation: scale, wall segments, interior fraction, status counts.
"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import room_geometry as rg  # noqa: E402

R5 = ("/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-mergeship-wt/"
      "nsai_board_round5_2026-09-04/results")
REPO = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-repo"
PDF = {
    "fishkill_397": os.path.join(REPO, "spike_samples", "397Fishkill.pdf"),
    "364_main": os.path.join(REPO, "spike_samples", "364Main.pdf"),
    "dutchess_livestock": os.path.join(
        REPO, "golden", "plans", "Dutchess_Livestock_Bidding_Documents.pdf"),
    "jw_hudson_hotel": os.path.join(
        REPO, "nsai_batch_2026-08-20", "hudson_hotel", "plans_clean.pdf"),
    "jw_homewood_suites": os.path.join(
        REPO, "nsai_batch_2026-08-20", "homewood_suites", "plans_clean.pdf"),
    "jw_under_canvas_ulum": os.path.join(
        REPO, "nsai_batch_2026-08-20", "under_canvas_ulum",
        "plans_clean.pdf"),
    "jw_harlem_valley": os.path.join(
        REPO, "nsai_batch_2026-08-20", "harlem_valley", "plans_clean.pdf"),
}


def anchors_by_page(analysis):
    """(page_idx0 -> [(name, x_pt, y_pt)]) from roster bboxes — the same
    construction as _compute_room_geometry_shadow."""
    out = {}
    for fl in analysis.get("floors") or []:
        for rm in fl.get("rooms") or []:
            bb = rm.get("bbox") or {}
            norm = bb.get("label_bbox_norm")
            size = bb.get("page_size_pt")
            page = rm.get("source_page")
            if not (norm and size and page):
                continue
            try:
                x0, y0, x1, y1 = norm
                W, Hh = size
                cx = (x0 + x1) / 2.0 * W
                cy = (y0 + y1) / 2.0 * Hh
            except Exception:
                continue
            name = str(rm.get("room_id") or rm.get("room_name") or "")
            out.setdefault(int(page) - 1, []).append((name, cx, cy))
    return out


def main(jobs):
    for job in jobs:
        path = os.path.join(R5, f"{job}.result.json")
        pdf = PDF.get(job)
        if not (os.path.exists(path) and pdf and os.path.exists(pdf)):
            print(f"SKIP {job}")
            continue
        a = json.load(open(path)).get("analysis", {})
        pages = anchors_by_page(a)
        print(f"\n=== {job} ({len(pages)} anchored pages) ===")
        for pg, anch in sorted(pages.items()):
            try:
                pts, src = None, "detect"
                try:
                    import vector_measure as vm
                    pts, src = vm.detect_scale_robust(pdf, pg)
                except Exception as e:
                    src = f"scale-err:{type(e).__name__}"
                line = f"  p{pg+1}: anchors={len(anch)} scale={pts} ({src})"
                if pts:
                    free, interior, outside, meta = rg.build_enclosure_grid(
                        pdf, pg, pts)
                    frac = interior.sum() / interior.size
                    line += (f" segs={meta['n_segments']} "
                             f"interior_frac={frac:.3f}")
                    res = rg.measure_room_areas(pdf, pg, anch, pts)
                    cnt = Counter(v["status"].split(":")[0]
                                  for v in res["rooms"].values())
                    line += f" statuses={dict(cnt)}"
                print(line)
            except Exception as e:
                print(f"  p{pg+1}: ERR {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["fishkill_397", "jw_hudson_hotel",
                          "jw_under_canvas_ulum", "jw_homewood_suites"])
