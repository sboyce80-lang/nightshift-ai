#!/usr/bin/env python3
"""Offline replay: VME ceilings gate against stored round-5 rosters.

Free — the rosters already carry the room-geometry shadow (prod flag on),
so this replays only the new gate and scores painted-ceiling totals
against the golden targets. The replay_wall_seam pattern.

Targets: Rider from REFERENCE_CASES / takeoff xlsx; JW summed from
ground_truth.json ceiling line items; Northwell from JW's markup key.
"""
import copy
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["NIGHTSHIFT_VME_CEILINGS"] = "1"
import Takeoff_DIRECT as T  # noqa: E402

R5 = ("/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-mergeship-wt/"
      "nsai_board_round5_2026-09-04/results")

TARGETS = {
    "364_main": 26839, "fishkill_397": 13451, "dutchess_livestock": 2061,
    "jw_hudson_hotel": 17704, "jw_caris_hyde_park": 6641,
    "jw_under_canvas_ulum": 2756, "jw_homewood_suites": 61176,
    "jw_harlem_valley": None, "honey_farms_malta": 1029,
    "academy_88": None,
}


def painted_room_sum(a):
    tot = 0.0
    for fl in a.get("floors") or []:
        for r in fl.get("rooms") or []:
            m = r.get("materials") or {}
            if (r.get("in_scope", True) and m.get("ceiling_painted")
                    and "dryfall" not in str(m.get("ceiling", "")).lower()):
                tot += (T._num((r.get("dimensions") or {})
                        .get("ceiling_area_sqft", 0))
                        * (T._num(r.get("unit_multiplier", 1)) or 1))
    return tot


rows = []
for path in sorted(glob.glob(os.path.join(R5, "*.result.json"))):
    job = os.path.basename(path).replace(".result.json", "")
    raw = json.load(open(path))
    a = copy.deepcopy(raw.get("analysis", raw))
    a.pop("_vme_ceilings", None)
    agg_before = T._num((a.get("aggregated_totals") or {})
                        .get("total_paintable_ceiling_sqft", 0))
    room_before = painted_room_sum(a)
    a = T._apply_vme_ceilings(a)
    v = a.get("_vme_ceilings") or {}
    agg_after = T._num((a.get("aggregated_totals") or {})
                       .get("total_paintable_ceiling_sqft", 0))
    tgt = TARGETS.get(job)
    d_before = (f"{(agg_before-tgt)/tgt*100:+.0f}%" if tgt else "n/a")
    d_after = (f"{(agg_after-tgt)/tgt*100:+.0f}%" if tgt else "n/a")
    rows.append((job, tgt, agg_before, agg_after, d_before, d_after,
                 v.get("applied"), v.get("reason", ""),
                 v.get("coverage"), v.get("rooms_replaced")))

print(f"{'job':26s} {'target':>8s} {'before':>9s} {'after':>9s} "
      f"{'Δbef':>7s} {'Δaft':>7s}  applied  detail")
for (job, tgt, b, aft, db, da, applied, why, cov, nrep) in rows:
    detail = (f"cov={cov} rooms={nrep}" if applied
              else str(why)[:60])
    print(f"{job:26s} {str(tgt or '—'):>8s} {b:>9,.0f} {aft:>9,.0f} "
          f"{db:>7s} {da:>7s}  {str(bool(applied)):7s}  {detail}")
