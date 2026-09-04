#!/usr/bin/env python3
"""Offline wall-seam replay: 4 stored Northwell rosters, zero API cost.

Reconstructs each roster's pre-gate state (un-marks rooms the roster
gates excluded, clears gate markers), re-runs the ACTUAL gates from
Takeoff_DIRECT (template-instance dedup, schedule room scope), then the
scoped measurement and the basis-2 promotion checks, and scores walls
against the JW key (20,308 SF). Run with NIGHTSHIFT_SHEET_INDEX_TITLES=1.

The promotion checks mirror the gate in Takeoff_DIRECT (share >= 0.6,
bill <= 1.75x max(post, pre-clip) expectation) — keep in sync by hand.
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

JW_WALLS = 20308.0
ROSTERS = {
    "rerun3":     "../nightshift-repo/nsai_jw_northwell_rerun3_2026-09-02/result.json",
    "rerun4":     "../nightshift-vmedetect-wt/nsai_jw_northwell_rerun4_2026-09-03/result.json",
    "combined-1": "nsai_jw_northwell_combined_2026-09-04/result.json",
    "combined-2": "nsai_jw_northwell_combined2_2026-09-04/result.json",
}
PDF = {
    "rerun3":     "../nightshift-repo/nsai_jw_northwell_rerun3_2026-09-02/plans_clean.pdf",
    "rerun4":     "../nightshift-vmedetect-wt/nsai_jw_northwell_rerun4_2026-09-03/plans_clean.pdf",
    "combined-1": "nsai_jw_northwell_combined_2026-09-04/plans_clean.pdf",
    "combined-2": "nsai_jw_northwell_combined2_2026-09-04/plans_clean.pdf",
}
RESET_REASONS = ("template-instance dedup", "outside the scheduled scope",
                 "duplicate of another extracted room")
GATE_MARKERS = ("_template_instance_dedup", "_schedule_room_scope",
                "_vme_scoped", "_vme_shadow_v2")


def preclip_roster(analysis):
    a = copy.deepcopy(analysis)
    for m in GATE_MARKERS:
        a.pop(m, None)
    for fl in a.get("floors") or []:
        for r in fl.get("rooms") or []:
            reason = str(r.get("scope_exclusion_reason") or "")
            if not r.get("in_scope", True) and any(
                    k in reason for k in RESET_REASONS):
                r["in_scope"] = True
                r["scope_exclusion_reason"] = ""
    return a


def replay(name, path):
    raw = json.load(open(path))
    analysis = raw.get("analysis", raw)
    a = preclip_roster(analysis)

    import Takeoff_DIRECT as td
    import vme_attribution as va
    a = td._dedup_template_instances(a)
    a = td._apply_schedule_room_scope(a)

    scoped = va.compute_vme_scoped([PDF[name]], a)
    bill_sf = scoped.get("measured_wall_sf") or 0
    bill_lf = scoped.get("measured_wall_run_lf") or 0
    share = scoped.get("region_lf_share") or 0
    post = scoped.get("frac_expectation_lf") or 0
    pre = scoped.get("frac_expectation_preclip_lf") or 0
    unmeasured = scoped.get("unmeasured") or []

    if unmeasured:
        verdict = f"ABSTAIN unmeasured {unmeasured}"
    elif bill_sf <= 0:
        verdict = "ABSTAIN no billable geometry"
    elif share < 0.6:
        verdict = f"ABSTAIN share {share:.2f} < 0.6"
    elif bill_lf > 1.75 * max(post, pre, 1.0):
        verdict = (f"ABSTAIN disagree bill {bill_lf:,.0f} > "
                   f"1.75x{max(post, pre):,.0f}")
    else:
        verdict = "PROMOTE"

    delta = (bill_sf - JW_WALLS) / JW_WALLS * 100
    n_rooms = sum(len(fl.get("rooms") or []) for fl in a.get("floors") or [])
    n_scope = sum(1 for fl in a.get("floors") or []
                  for r in fl.get("rooms") or [] if r.get("in_scope", True))
    print(f"{name:11s} rooms {n_scope}/{n_rooms} in-scope | "
          f"walls {bill_sf:>7,.0f} SF ({delta:+.1f}%) share {share:.2f} "
          f"expect {post:,.0f}/{pre:,.0f} | {verdict}")
    return verdict, delta


if __name__ == "__main__":
    os.environ.setdefault("NIGHTSHIFT_SHEET_INDEX_TITLES", "1")
    os.environ.setdefault("NIGHTSHIFT_TEMPLATE_INSTANCE_DEDUP", "1")
    os.environ.setdefault("NIGHTSHIFT_SCHEDULE_ROOM_SCOPE", "1")
    for name, path in ROSTERS.items():
        if not os.path.exists(path):
            print(f"{name}: MISSING {path}")
            continue
        try:
            replay(name, path)
        except Exception as e:
            import traceback
            print(f"{name}: ERROR {e!r}")
            traceback.print_exc()
