"""Regression tests for the PNC Milwaukee P0 fixes (2026-07-06).

Scott Redmond's validated feedback on the PNC 15th-floor estimate exposed:

  (1) I-SERIES SHEETS UNRECOGNIZED — bare 'I' (Interiors) was missing from
      _DISCIPLINE_MAP, so I101/I111/I501/I601 fell back to stray 22pt "A1"
      grid labels. The I601 room finish schedule was never attributed.
  (2) FINISH SCHEDULE NEVER REACHED THE MODEL — _extract_room_finish_schedule
      sent chunk 1 of the whole set; I601 (18 pages into the filtered set)
      was never in it. Now targeted via _find_finish_schedule_pages.
  (3) DOORS: 71 plan-symbol doors priced full-paint ($11,555) though the
      A601 schedule read "aluminum storefront, factory finished" (truth: 5
      wood doors). New flag-gated _reconcile_door_materials_vs_plan makes
      the schedule authoritative when it reports fewer paintable doors.

Offline, no API (the extraction test uses a fake client).
"""
import io
import json
import os
import tempfile

import fitz

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


# ── Fix 1: I-series discipline recognition ──────────────────────────────────
print("\nFix 1 — bare 'I' (Interiors) sheets recognized")


def _lookup(prefix):
    for dp, name, inc in T._DISCIPLINE_MAP:
        if prefix == dp or prefix.startswith(dp):
            return name, inc
    return None, None


check(_lookup("I") == ("Interiors", True), "prefix I -> Interiors, included")
check(_lookup("ID") == ("Interior Design", True), "prefix ID unchanged")
check(_lookup("IN") == ("Interior", True), "prefix IN unchanged")
check(_lookup("E") == ("Electrical", False), "prefix E still excluded")

# End-to-end: a page whose 45pt title-block ID is I601 must classify as
# Interiors, not fall back to the smaller stray "A1" label.
tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
tmp.close()
doc = fitz.open()
pg = doc.new_page(width=1224, height=792)
pg.insert_text((1100, 750), "I601", fontsize=45)
pg.insert_text((300, 300), "A1", fontsize=22)
pg.insert_text((100, 100), "INTERIOR FINISH SCHEDULE", fontsize=17)
doc.save(tmp.name)
doc.close()
cls = T._classify_pdf_pages(tmp.name)
check(cls and cls[0]["sheet_number"] == "I601",
      f"title-block I601 wins over stray A1 (got {cls[0]['sheet_number'] if cls else None})")
check(cls and cls[0]["include"],
      "I-series page is included for extraction")
os.unlink(tmp.name)

# ── Fix 2: targeted finish-schedule pages ───────────────────────────────────
print("\nFix 2 — finish schedule extraction targets the right pages")
tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
tmp.close()
doc = fitz.open()
for i in range(6):
    pg = doc.new_page(width=1224, height=792)
    pg.insert_text((72, 72), f"floor plan sheet {i}", fontsize=10)
pg = doc.new_page(width=1224, height=792)  # page 7 = the schedule
pg.insert_text((100, 100), "INTERIOR FINISH SCHEDULE", fontsize=17)
pg.insert_text((100, 200), "room finish schedule", fontsize=8)
# page 8: small-font sheet-note callout only (the PNC false-positive shape)
pg = doc.new_page(width=1224, height=792)
pg.insert_text((100, 100), "WORK. REFER TO FINISH SCHEDULE.", fontsize=8.5)
doc.save(tmp.name)
doc.close()

pages = T._find_finish_schedule_pages(tmp.name)
check(pages == [6],
      f"titled page wins; 8.5pt callout page ignored (got {pages})")


class _FakeStream:
    def __init__(self, text):
        self.text_stream = iter([text])
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeClient:
    """Captures the PDF payload and returns a canned finish schedule."""
    def __init__(self):
        self.sent_pdf_b64 = None
        outer = self

        class _Messages:
            def stream(self, **kw):
                for blk in kw["messages"][0]["content"]:
                    if blk.get("type") == "document":
                        outer.sent_pdf_b64 = blk["source"]["data"]
                return _FakeStream(json.dumps({
                    "room_finish_schedule": [
                        {"room_name": "Office", "room_number": "1501",
                         "wall_finish": "PT-1", "ceiling_finish": "ACT",
                         "base_finish": "WB-1 Wood Base",
                         "floor_finish": "CPT", "unit_type": "common_area",
                         "floor_level": "15", "is_common_area": True}],
                    "structural_finish_scope": [],
                    "building_info": {}, "notes": []}))
        self.messages = _Messages()


import base64
fake = _FakeClient()
out = T._extract_room_finish_schedule(fake, tmp.name, page_indices=pages)
check(out is not None and len(out.get("room_finish_schedule", [])) == 1,
      "targeted extraction returns the schedule rows")
sent = fitz.open(stream=base64.b64decode(fake.sent_pdf_b64), filetype="pdf")
check(len(sent) == 1 and "FINISH SCHEDULE" in sent[0].get_text(),
      f"model received ONLY the schedule page (got {len(sent)} page(s))")
sent.close()
os.unlink(tmp.name)

# ── Fix 3: door material reconcile (schedule authoritative over plan) ───────
print("\nFix 3 — _reconcile_door_materials_vs_plan")


def _pnc_analysis():
    return {
        "has_door_schedule": True,
        "aggregated_totals": {"total_doors_full_paint": 71,
                              "total_doors_hm_panel": 0,
                              "total_doors_frame_only": 0},
        "schedule_data": {"door_schedule": {
            "total_doors_full_paint": 0, "total_doors_hm_panel": 0,
            "door_marks_counted": [],
            "notes": "aluminum storefront systems, factory finished"}},
        "_schedule_authoritative_counts": {"total_doors_full_paint": 71},
        "notes": [],
    }


os.environ["NIGHTSHIFT_DOOR_MATERIAL_RECONCILE"] = "1"

a = T._reconcile_door_materials_vs_plan(_pnc_analysis())
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 0,
      "PNC shape: 71 plan doors -> 0 (schedule says factory-finished)")
check(a["_schedule_authoritative_counts"]["total_doors_full_paint"] == 0,
      "authoritative stash synced so recalc can't resurrect 71")
check(any(n.startswith("[Door Schedule Scope]") for n in a["notes"]),
      "reconcile note added")
check(any(n.startswith("[RFI: Door Scope]") and "71" in n for n in a["notes"]),
      "RFI added naming the 71-door delta")
check(a.get("_door_material_reconcile", {}).get("excluded_delta") == 71,
      "record captures the excluded delta")
n_notes = len(a["notes"])
a2 = T._reconcile_door_materials_vs_plan(a)
check(len(a2["notes"]) == n_notes, "idempotent on second call")

# Partial schedule: 5 wood doors confirmed -> adopt 5, not 0.
a = _pnc_analysis()
a["schedule_data"]["door_schedule"]["total_doors_full_paint"] = 5
a = T._reconcile_door_materials_vs_plan(a)
check(a["aggregated_totals"]["total_doors_full_paint"] == 5,
      "schedule with 5 paintable doors -> adopt 5")

# Guards.
a = _pnc_analysis()
a["schedule_data"]["door_schedule"] = {"notes": "unreadable"}
a = T._reconcile_door_materials_vs_plan(a)
check(a["aggregated_totals"]["total_doors_full_paint"] == 71,
      "no structured totals in schedule -> untouched")

a = _pnc_analysis()
a["has_door_schedule"] = False
a = T._reconcile_door_materials_vs_plan(a)
check(a["aggregated_totals"]["total_doors_full_paint"] == 71,
      "no door schedule flag -> untouched")

a = _pnc_analysis()
a["schedule_data"]["door_schedule"]["total_doors_full_paint"] = 80
a = T._reconcile_door_materials_vs_plan(a)
check(a["aggregated_totals"]["total_doors_full_paint"] == 71,
      "schedule reporting MORE doors than plan -> never adjusts upward")

os.environ["NIGHTSHIFT_DOOR_MATERIAL_RECONCILE"] = "0"
a = T._reconcile_door_materials_vs_plan(_pnc_analysis())
check(a["aggregated_totals"]["total_doors_full_paint"] == 71,
      "flag off -> untouched")

# Shape 1 (INNIO hm_panel fix) must be unaffected by the new hook.
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_FIX"] = "0"
a = T._reconcile_door_schedule_scope(_pnc_analysis())
check(a["aggregated_totals"]["total_doors_full_paint"] == 71,
      "choke-point wrapper with both flags off -> untouched")
os.environ.pop("NIGHTSHIFT_DOOR_SCHEDULE_FIX", None)
os.environ.pop("NIGHTSHIFT_DOOR_MATERIAL_RECONCILE", None)


print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
