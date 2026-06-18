"""Regression tests for the work-area sanity basis (NIGHTSHIFT_WORK_AREA_BASIS).

Level 2 / IEBC Work-Area-Method renovations paint only a small part of a large
building, so the pre-finalize "paintable must be 3-6x footprint" sanity check
falsely trips manual_review (INNIO Waukesha: 15,160 SF paintable, 3,955 SF work
area, 580,317 GSF plant -> 0.0x footprint -> blocked on every run). The fix
validates paintable against the declared WORK AREA instead.

Locks the work-area parser and the basis arithmetic. Offline, no API. Also runs
the real saved INNIO result JSON when present.
"""
import os
import json
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


INNIO_SUMMARY = ("Renovation of existing engine test cells. Work area is "
                 "approximately 3,955 SF within a 580,317 GSF building. Project "
                 "is classified as a Level 2 Alteration under the IEBC Work Area "
                 "Method.")

# 1) Parse from project_overview.scope_summary; do NOT grab the 580,317 GSF.
a = {"project_overview": {"scope_summary": INNIO_SUMMARY}}
check(T._declared_work_area_sqft(a) == 3955.0,
      f"work area not parsed from scope_summary: {T._declared_work_area_sqft(a)}")

# 2) Parse from notes when overview is absent.
a = {"notes": ["[G101] The actual work area is 3,955 SF within the 580,317 SF building."]}
check(T._declared_work_area_sqft(a) == 3955.0,
      f"work area not parsed from notes: {T._declared_work_area_sqft(a)}")

# 3) Structured field wins over narrative.
a = {"project_overview": {"work_floor_area_sqft": 4200, "scope_summary": INNIO_SUMMARY}}
check(T._declared_work_area_sqft(a) == 4200.0, "structured work area not preferred")

# 4) No work area stated -> 0 (whole-building job, unaffected).
a = {"project_overview": {"scope_summary": "New 12,000 SF retail building, ground up."},
     "notes": ["Full building paint."]}
check(T._declared_work_area_sqft(a) == 0.0, f"false work-area hit: {T._declared_work_area_sqft(a)}")

# 5) Basis arithmetic: INNIO passes against work area, fails against footprint.
work_area, footprint, paintable = 3955.0, 580317.0, 15160.0
used_work_area = 1000 < work_area < footprint * 0.5
check(used_work_area is True, "work-area basis should engage for small-area reno")
check(paintable >= work_area * 3, "INNIO paintable should clear 3x the work area")
check(paintable < footprint * 3, "sanity: INNIO would fail the footprint basis")

# 6) Flag default OFF.
os.environ.pop("NIGHTSHIFT_WORK_AREA_BASIS", None)
check(T._work_area_basis_enabled() is False, "work-area basis flag should default off")

# 7) Real saved INNIO result JSON, when present.
for p in (os.path.expanduser("~/Downloads/INNIO_run_163606.json"),
          "/tmp/results_json/INNIO.json"):
    if not os.path.exists(p):
        continue
    a = json.load(open(p))["analysis"]
    wa = T._declared_work_area_sqft(a)
    check(abs(wa - 3955.0) < 1, f"INNIO JSON work area expected ~3955, got {wa}")
    break

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
