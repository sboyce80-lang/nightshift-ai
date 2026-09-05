#!/usr/bin/env python3
"""Deterministic door ledger (door_ledger.py + NIGHTSHIFT_DOOR_SCHEDULE_LEDGER).

Locks in: Mode A parses a synthetic text door schedule (count, material
split, wrapped-row dedup); the tag-mark collision rule skips marks equal
to room labels; the pipeline gate is inert with the flag off, gives the
parsed schedule authority over extraction counts, RFIs on >25% delta,
keeps Mode B strictly diagnostic, and can never fail the job.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fitz  # noqa: E402
import door_ledger as DL  # noqa: E402
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


print("door ledger checks")

# ── Mode A on a synthetic schedule page ─────────────────────────────────────
doc = fitz.open()
page = doc.new_page(width=612, height=792)
page.insert_text((200, 40), "DOOR SCHEDULE", fontsize=14)
page.insert_text((40, 70), "DOOR", fontsize=9)
page.insert_text((100, 70), "SIZE", fontsize=9)
page.insert_text((180, 70), "TYPE", fontsize=9)
page.insert_text((240, 70), "MATL", fontsize=9)
page.insert_text((320, 70), "FRAME", fontsize=9)
page.insert_text((400, 70), "REMARKS", fontsize=9)
rows = [
    ("101", "3070", "A", "WD", "HM", ""),
    ("102", "3070", "A", "WD", "HM", ""),
    ("103", "6070", "B", "HM", "HM", "PAIR"),
    ("104", "3070", "A", "AL", "AL", "STOREFRONT"),
    ("105", "3070", "C", "WD", "HM", ""),
    ("105", "", "", "", "", "SEE NOTE 3"),   # wrapped continuation row
    ("106", "3070", "A", "HM", "HM", ""),
]
y = 90
for mark, size, typ, matl, frame, rem in rows:
    for x, txt in ((40, mark), (100, size), (180, typ), (240, matl),
                   (320, frame), (400, rem)):
        if txt:
            page.insert_text((x, y), txt, fontsize=9)
    y += 16
sched_pdf = os.path.join(HERE, ".cache_test_door_sched.pdf")
doc.save(sched_pdf)
doc.close()

parsed = DL.parse_schedule_pages([sched_pdf])
marks = [e["mark"] for e in parsed["entries"]]
check(len(parsed["entries"]) == 6,
      f"6 unique doors parsed (wrapped row deduped): got {len(marks)} "
      f"{marks}")
by_mark = {e["mark"]: e for e in parsed["entries"]}
check(by_mark.get("103", {}).get("paint_class") == "hm_panel",
      "HM material classifies hm_panel")
check(by_mark.get("101", {}).get("paint_class") == "full_paint",
      "WD material classifies full_paint")
check(by_mark.get("104", {}).get("paint_class") == "excluded",
      "AL storefront classifies excluded")

led = DL.build_door_ledger([sched_pdf])
check(led["mode"] == "schedule", f"ledger picks mode A: {led['mode']}")
check(led["count"] == 5,
      f"excluded doors leave the count (6 - 1 AL = 5): {led['count']}")
check(led["full_paint"] == 3 and led["hm_panel"] == 2,
      f"split 3 WD / 2 HM: {led['full_paint']}/{led['hm_panel']}")

# ── collision rule ──────────────────────────────────────────────────────────
doc2 = fitz.open()
p2 = doc2.new_page(width=612, height=792)
p2.insert_text((300, 30), "FLOOR PLAN", fontsize=12)
# door tag 201A in a small circle; room label 201 in another small circle
p2.draw_circle(fitz.Point(100, 100), 12)
p2.insert_text((92, 104), "201A", fontsize=7)
p2.draw_circle(fitz.Point(200, 100), 12)
p2.insert_text((194, 104), "201", fontsize=7)
plan_pdf = os.path.join(HERE, ".cache_test_door_plan.pdf")
doc2.save(plan_pdf)
doc2.close()

d3 = fitz.open(plan_pdf)
tags = DL.count_tag_marks(d3[0], room_labels={"201"})
d3.close()
check("201A" in tags, f"door tag inside shape is counted: {tags}")
check("201" not in tags,
      "mark equal to a room label is a collision, not a door")

# ── pipeline gate ───────────────────────────────────────────────────────────


def _analysis(fp=40.0, hm=10.0, paths=("x.pdf",)):
    return {"aggregated_totals": {"total_doors_full_paint": fp,
                                  "total_doors_hm_panel": hm},
            "_vme_pdf_paths": list(paths), "notes": [], "rfi_items": []}


class _StubDL:
    def __init__(self, result):
        self.result = result

    def build_door_ledger(self, paths):
        return self.result


def _run_gate(analysis, stub):
    sys.modules["door_ledger"] = stub
    try:
        return T._apply_door_schedule_ledger(analysis)
    finally:
        sys.modules["door_ledger"] = DL


os.environ.pop("NIGHTSHIFT_DOOR_SCHEDULE_LEDGER", None)
a = _analysis()
a = _run_gate(a, _StubDL({"mode": "schedule", "count": 80,
                          "full_paint": 60, "hm_panel": 20}))
check(a["aggregated_totals"]["total_doors_full_paint"] == 40.0,
      "flag off: gate fully inert")
check("_door_ledger" not in a, "flag off: no ledger record")

os.environ["NIGHTSHIFT_DOOR_SCHEDULE_LEDGER"] = "1"

# Mode A with ledger materials: ledger count + ledger split win
a = _analysis(fp=40.0, hm=10.0)
a = _run_gate(a, _StubDL({"mode": "schedule", "count": 78,
                          "full_paint": 52, "hm_panel": 26,
                          "sources": ["schedule(p31)"]}))
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 52.0 and
      agg["total_doors_hm_panel"] == 26.0,
      f"ledger count + material split authoritative: "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
check(any("[Door Ledger] 78 doors" in n for n in a["notes"]),
      "provenance note names the ledger source")
check(any(r.get("category") == "Door Count" for r in a["rfi_items"]),
      "78 vs 50 (>25% delta) raises the Door Count RFI")
check(a["_door_ledger"]["extraction_doors_at_gate"] == 50.0,
      "record keeps what extraction had at the gate")

# Mode A without materials: extraction's ratio classifies the count
a = _analysis(fp=40.0, hm=10.0)
a = _run_gate(a, _StubDL({"mode": "schedule", "count": 60,
                          "full_paint": 0, "hm_panel": 0,
                          "sources": ["schedule(p4)"]}))
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 48.0 and
      agg["total_doors_hm_panel"] == 12.0,
      f"no-material ledger: extraction's 4:1 ratio splits 60: "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
check(not any(r.get("category") == "Door Count" for r in a["rfi_items"]),
      "60 vs 50 (20% delta) stays under the RFI threshold")

# Mode B: diagnostic only, counts never move
a = _analysis(fp=40.0, hm=10.0)
a = _run_gate(a, _StubDL({"mode": "symbols", "count": 14,
                          "detector": "tag_marks"}))
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 40.0 and
      agg["total_doors_hm_panel"] == 10.0,
      "mode B never changes counts")
check(any("diagnostic only" in n for n in a["notes"]),
      "mode B gross divergence gets a diagnostic note")

# Small ledgers grant no authority
a = _analysis(fp=40.0, hm=10.0)
a = _run_gate(a, _StubDL({"mode": "schedule", "count": 3,
                          "full_paint": 3, "hm_panel": 0}))
check(a["aggregated_totals"]["total_doors_full_paint"] == 40.0,
      "a 3-entry schedule (<5) is not authoritative")

# Crash resilience
a = _analysis()

class _Boom:
    def build_door_ledger(self, paths):
        raise RuntimeError("boom")

a = _run_gate(a, _Boom())
check(a["aggregated_totals"]["total_doors_full_paint"] == 40.0 and
      "error" in a.get("_door_ledger", {}),
      "ledger crash records an error and changes nothing")

for f in (sched_pdf, plan_pdf):
    try:
        os.remove(f)
    except OSError:
        pass

print()
if fails:
    print(f"❌ {len(fails)} door ledger check(s) failed")
    sys.exit(1)
print("✅ all door ledger checks passed")
