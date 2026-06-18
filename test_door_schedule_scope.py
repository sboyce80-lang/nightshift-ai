"""Regression tests for _reconcile_door_schedule_scope (NIGHTSHIFT_DOOR_SCHEDULE_FIX).

Locks in the 2026-06-18 INNIO Waukesha (Devine Painting beta) door fix, where the
door schedule lived on an interior-elevations sheet (A404) and:
  (1) the extractor reasoned to the correct leaf count in prose ("total = 5
      leaves") but emitted a stale structured total (6), with doors_by_floor
      (2+4) inconsistent with the counted marks (11/14/15) — recover the true
      count from the per-mark notes, ONLY when parsed marks == door_marks_counted.
  (2) new HM doors in new HM frames were filed hm_panel even though scope said
      "New hollow metal doors and frames" — reclassify hm_panel->full_paint.
Guards: flag-off is a no-op; mixed/already-classified schedules are untouched;
count recovery never fires when the parsed marks don't match the counted marks.
Offline, no API. Also runs the real saved INNIO JSON when present.
"""
import os
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_FIX"] = "1"
import json
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _an(scope, hm_panel, ds_notes, marks, fp=0, fr=0, building="commercial/industrial"):
    return {
        "project_info": {"building_type": building, "scope_notes": scope},
        "aggregated_totals": {
            "total_doors_full_paint": fp,
            "total_doors_hm_panel": hm_panel,
            "total_doors_frame_only": fr,
        },
        "schedule_data": {"door_schedule": {
            "total_doors_hm_panel": hm_panel,
            "door_marks_counted": marks,
            "notes": ds_notes,
        }},
        "notes": [],
    }


INNIO_NOTES = ("Mark 11: Qty 2, Type F, Panel HM, Frame HM, HW-01. "
               "Mark 14: Qty 1, Panel HM, Frame 01A (HM), HW-02. "
               "Mark 15: Qty 2, Panel HM, Frame HM, HW-01. Total = 5 leaves.")
INNIO_SCOPE = ("new and existing walls of sheet A401, New hollow metal doors "
               "and frames, columns")

# 1) INNIO case: 6 hm_panel -> recover 5 leaves -> reclassify to full_paint.
a = _an(INNIO_SCOPE, 6, INNIO_NOTES, ["11", "14", "15"])
T._reconcile_door_schedule_scope(a)
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 5, f"expected 5 full_paint, got {agg['total_doors_full_paint']}")
check(agg["total_doors_hm_panel"] == 0, f"expected 0 hm_panel, got {agg['total_doors_hm_panel']}")
rec = a.get("_door_schedule_scope_fix", {})
check(rec.get("leaf_count", {}).get("to") == 5, "leaf_count not recovered to 5")
check(rec.get("reclassified", {}).get("hm_panel_to_full_paint") == 5, "reclassify count wrong")

# 2) Frames NOT in scope -> count still corrected, but stays hm_panel.
a = _an("paint walls and HM door panels only", 6, INNIO_NOTES, ["11", "14", "15"])
T._reconcile_door_schedule_scope(a)
agg = a["aggregated_totals"]
check(agg["total_doors_hm_panel"] == 5, f"expected 5 hm_panel (no frame paint), got {agg['total_doors_hm_panel']}")
check(agg["total_doors_full_paint"] == 0, "should not reclassify when frames not in scope")

# 3) Count-recovery guard: parsed marks != door_marks_counted -> no count change.
a = _an(INNIO_SCOPE, 6, "Mark 11: Qty 2. Mark 14: Qty 1.", ["11", "14", "15"])
T._reconcile_door_schedule_scope(a)
agg = a["aggregated_totals"]
# frames-painted still reclassifies the (uncorrected) 6 -> full_paint
check(agg["total_doors_full_paint"] == 6, f"unguarded count recovery fired, got {agg['total_doors_full_paint']}")
check("leaf_count" not in a.get("_door_schedule_scope_fix", {}), "leaf_count should be absent when marks mismatch")

# 4) Mixed bucket (some full_paint already) -> untouched (conservative).
a = _an(INNIO_SCOPE, 6, INNIO_NOTES, ["11", "14", "15"], fp=3)
T._reconcile_door_schedule_scope(a)
check("_door_schedule_scope_fix" not in a, "mixed-bucket schedule should be left untouched")

# 5) Flag OFF -> no-op.
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_FIX"] = "0"
a = _an(INNIO_SCOPE, 6, INNIO_NOTES, ["11", "14", "15"])
T._reconcile_door_schedule_scope(a)
check(a["aggregated_totals"]["total_doors_hm_panel"] == 6, "flag-off should be a no-op")
check("_door_schedule_scope_fix" not in a, "flag-off should not stamp record")
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_FIX"] = "1"

# 6) Idempotent — second pass changes nothing.
a = _an(INNIO_SCOPE, 6, INNIO_NOTES, ["11", "14", "15"])
T._reconcile_door_schedule_scope(a)
snapshot = dict(a["aggregated_totals"])
T._reconcile_door_schedule_scope(a)
check(a["aggregated_totals"] == snapshot, "second pass mutated totals (not idempotent)")

# 7) Real saved INNIO JSON (when present) — 6 hm_panel -> 5 full_paint.
for p in ("/tmp/results_json/INNIO.json",
          os.path.expanduser("~/Downloads/construction_analysis_20260617_CORRECTED.json")):
    if not os.path.exists(p):
        continue
    an = json.load(open(p))["analysis"]
    an.pop("_door_schedule_scope_fix", None)
    T._reconcile_door_schedule_scope(an)
    agg = an["aggregated_totals"]
    check(agg["total_doors_full_paint"] == 5 and agg["total_doors_hm_panel"] == 0,
          f"INNIO JSON expected 5 full_paint/0 hm_panel, got "
          f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
    break

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
