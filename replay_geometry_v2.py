#!/usr/bin/env python3
"""End-to-end v1-vs-v2 scoreboard: rebuild room-geometry shadows from the
plan PDFs (both modes), run the VME ceilings gate on each, score vs the
golden ceiling targets. Offline, free.
"""
import copy
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import room_geometry as rg  # noqa: E402
import Takeoff_DIRECT as T  # noqa: E402
from diag_room_geometry import anchors_by_page, PDF, R5  # noqa: E402

TARGETS = {
    "364_main": 26839, "fishkill_397": 13451, "dutchess_livestock": 2061,
    "jw_hudson_hotel": 17704, "jw_harlem_valley": None,
    "jw_homewood_suites": 61176, "jw_under_canvas_ulum": 2756,
}

os.environ["NIGHTSHIFT_VME_CEILINGS"] = "1"


def rebuild_shadow(pdf, pages, v2):
    os.environ["NIGHTSHIFT_ROOM_GEOMETRY_V2"] = "1" if v2 else "0"
    abp = {(pdf, pg): anch for pg, anch in pages.items()}
    return rg.compute_room_geometry_shadow([pdf], anchors_by_page=abp)


def main():
    jobs = sys.argv[1:] or list(TARGETS)
    print(f"{'job':22s} {'mode':3s} {'meas':>5s} {'merg':>5s} {'onw':>5s} "
          f"{'leak':>5s}  {'ceil_after':>10s} {'target':>8s} {'Δ':>7s} "
          f"applied/why")
    for job in jobs:
        rpath = os.path.join(R5, f"{job}.result.json")
        pdf = PDF.get(job)
        if not (pdf and os.path.exists(pdf) and os.path.exists(rpath)):
            print(f"{job:22s} SKIP (missing pdf or roster)")
            continue
        base = json.load(open(rpath)).get("analysis", {})
        pages = anchors_by_page(base)
        for v2 in (False, True):
            shadow = rebuild_shadow(pdf, pages, v2)
            cnt = Counter()
            for pg in shadow["pages"]:
                for v in (pg.get("rooms") or {}).values():
                    cnt[v["status"].split(":")[0]] += 1
            a = copy.deepcopy(base)
            a["_room_geometry_shadow"] = shadow
            a.pop("_vme_ceilings", None)
            a = T._apply_vme_ceilings(a)
            v = a.get("_vme_ceilings") or {}
            after = T._num((a.get("aggregated_totals") or {})
                           .get("total_paintable_ceiling_sqft", 0))
            tgt = TARGETS.get(job)
            d = f"{(after - tgt) / tgt * 100:+.0f}%" if tgt else "n/a"
            why = ("applied cov=%s ratio=%s" % (v.get("coverage"),
                                                v.get("ratio"))
                   if v.get("applied") else str(v.get("reason", ""))[:48])
            print(f"{job:22s} {'v2' if v2 else 'v1':3s} "
                  f"{cnt.get('measured', 0):>5d} {cnt.get('merged', 0):>5d} "
                  f"{cnt.get('on_wall', 0):>5d} {cnt.get('leaked', 0):>5d}  "
                  f"{after:>10,.0f} {str(tgt or '—'):>8s} {d:>7s} {why}")


if __name__ == "__main__":
    main()
