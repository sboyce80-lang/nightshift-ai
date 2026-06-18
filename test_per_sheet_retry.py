"""Regression tests for the per-sheet conditional retry-on-collapse helpers
(NIGHTSHIFT_PER_SHEET_RETRY).

Per-sheet extraction bypasses the multi-pass median, so a single pass that
anchors rooms to elevation/RCP views (walls -> ~0) has no safety net. The
INNIO Waukesha 2026-06-18 re-runs proved it: same PDF/code gave 7,942 SF GYP
wall one run, 80 SF the next. The retry re-runs per-sheet on a suspected
collapse and keeps the pass with the most measured wall geometry.

These lock the two deterministic primitives that drive the retry:
  _per_sheet_collapse_suspected — fires when >=2 GYP-scope rooms exist but
    total GYP wall is ~0 (CMU/exposed rooms excluded; collapse is GYP-specific).
  _per_sheet_geometry_score — total in-scope wall area; max picks the richest
    (least-collapsed) pass.
Guards: all-CMU jobs don't false-trigger; <2 GYP rooms don't trigger; a healthy
pass scores higher than a collapsed one. Offline, no API. Also runs the real
saved INNIO run1/run2 JSONs when present.
"""
import os
import json
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _room(name, walls, wall_sqft, in_scope=True):
    return {"room_name": name, "in_scope": in_scope,
            "materials": {"walls": walls},
            "dimensions": {"wall_area_sqft": wall_sqft}}


def _an(rooms):
    return {"floors": [{"floor_name": "1", "rooms": rooms}]}


# 1) Collapse: 3 GYP rooms but walls ~0 (anchored to elevation/RCP, no perim).
a = _an([
    _room("Test Cell GYP Ceiling Zone", "GYP", 0),
    _room("GYP Bulkhead", "GYP", 80),
    _room("275 Area", "GYP", 0),
    _room("Work Cafe", "CMU", 1815),  # CMU excluded from the GYP signal
])
check(T._per_sheet_collapse_suspected(a) is True, "collapse not detected (GYP walls ~0)")

# 2) Healthy: GYP rooms carry real wall area -> no collapse.
a = _an([
    _room("Test Cell North Zone", "GYP", 2880),
    _room("Production Break Room", "GYP", 990),
    _room("Work Cafe", "CMU", 1716),
])
check(T._per_sheet_collapse_suspected(a) is False, "healthy pass wrongly flagged as collapse")

# 3) All-CMU job: zero GYP-scope rooms -> never triggers (no false retry).
a = _an([_room("Plant Floor", "CMU", 5000), _room("Corridor", "CMU exposed block", 700)])
check(T._per_sheet_collapse_suspected(a) is False, "all-CMU job false-triggered collapse")

# 4) Fewer than 2 GYP-scope rooms -> not enough signal, no trigger.
a = _an([_room("Lone GYP", "GYP", 0), _room("Work Cafe", "CMU", 1716)])
check(T._per_sheet_collapse_suspected(a) is False, "single GYP room triggered collapse")

# 5) Geometry score: sums in-scope wall area, ignores out-of-scope.
a = _an([
    _room("A", "GYP", 2880),
    _room("B", "CMU", 1716),
    _room("C", "GYP", 500, in_scope=False),  # excluded
])
check(T._per_sheet_geometry_score(a) == 4596.0, f"geometry score wrong: {T._per_sheet_geometry_score(a)}")

# 6) Best-pass selection: a healthy pass scores higher than a collapsed one.
collapsed = _an([_room("GYP Ceiling Zone", "GYP", 0), _room("GYP Bulkhead", "GYP", 80),
                 _room("Cafe", "CMU", 1815)])
healthy = _an([_room("North Zone", "GYP", 2880), _room("South Zone", "GYP", 3100),
               _room("Cafe", "CMU", 1716)])
check(T._per_sheet_geometry_score(healthy) > T._per_sheet_geometry_score(collapsed),
      "healthy pass did not outscore collapsed pass")

# 7) Flag default OFF.
os.environ.pop("NIGHTSHIFT_PER_SHEET_RETRY", None)
check(T._per_sheet_retry_enabled() is False, "retry flag should default off")

# 8) Real saved INNIO run1 (good) / run2 (collapsed) JSONs, when present.
PAIRS = [("run1", "~/Downloads/INNIO_run1_135129.json", False),
         ("run2", "~/Downloads/INNIO_run2_151545.json", True)]
seen = []
for tag, p, want_collapse in PAIRS:
    p = os.path.expanduser(p)
    if not os.path.exists(p):
        continue
    a = json.load(open(p))["analysis"]
    got = T._per_sheet_collapse_suspected(a)
    check(got is want_collapse, f"INNIO {tag}: collapse expected {want_collapse}, got {got}")
    seen.append((tag, T._per_sheet_geometry_score(a)))
if len(seen) == 2:
    check(seen[0][1] > seen[1][1], "INNIO run1 score should exceed run2")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
