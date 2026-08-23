#!/usr/bin/env python3
"""Fixes for the Fishkill 397 phantom-exterior regression (2026-08-22).

Three independent guards, each flag-gated:
  A. _enforce_exterior_evidence: per-item negative evidence — a
     factory-finished / not-field-painted note zeroes the item it names
     even when job-level paint evidence exists elsewhere.
  B. will_synthesis._is_documented_scope_removal: the hard-numbers guard
     stops blocking Will's documented reductions-to-zero on exterior lines
     (NIGHTSHIFT_WILL_SCOPE_REMOVAL).
  C. _identify_elevation_pages: with NIGHTSHIFT_ELEV_REQUIRE_SHEETS the
     exterior pass abstains unless a REAL elevation sheet is present
     (validated against the actual Fishkill + Caris PDFs).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FISHKILL_PDF = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-repo/" \
               "spike_samples/397Fishkill.pdf"
CARIS_PDF = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-repo/" \
            "nsai_batch_2026-08-20/caris_hyde_park/plans_clean.pdf"

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def _clear():
    for k in ("NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE",
              "NIGHTSHIFT_WILL_SCOPE_REMOVAL",
              "NIGHTSHIFT_ELEV_REQUIRE_SHEETS"):
        os.environ.pop(k, None)


import Takeoff_DIRECT as T  # noqa: E402
import will_synthesis as W  # noqa: E402

FISHKILL_NOTES = (
    "Primary cladding is HardiePanel siding (Pearl Gray) and Hardie "
    "Artisan Square Channel siding (Mountain Sage) — a factory-finished "
    "fiber cement product not field-painted; AZEK painted trim (cornice, "
    "corner boards, trim) is called out as 'Painted AZEK' in multiple "
    "colors. Painted steel exposed lintels at brick window openings are "
    "noted as Color: Black.")


def _fishkill_analysis():
    return {
        "exterior": {"hardie_siding_sqft": 14800, "cornice_lf": 320,
                     "azek_trim_lf": 485, "corner_board_lf": 120,
                     "steel_lintel_lf": 180, "notes": FISHKILL_NOTES,
                     "paint_evidence": "'Painted AZEK' called out in "
                                       "multiple colors"},
        "aggregated_totals": {},
    }


print("— FIX A: per-item negative evidence —")
_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
a = T._enforce_exterior_evidence(_fishkill_analysis())
ext = a["exterior"]
check(ext["hardie_siding_sqft"] == 0,
      f"factory-finished hardie not zeroed: {ext['hardie_siding_sqft']}")
check(ext["azek_trim_lf"] == 485,
      f"painted AZEK wrongly zeroed: {ext['azek_trim_lf']}")
check(ext["steel_lintel_lf"] == 180,
      f"painted lintels wrongly zeroed: {ext['steel_lintel_lf']}")
rec = a.get("_exterior_evidence_gate") or {}
check("hardie_siding_sqft" in (rec.get("zeroed_negative") or {}),
      f"gate record missing zeroed_negative: {rec}")
check(any("factory-finished" in str(r).lower() or
          "factory finished" in str(r).lower()
          for r in (a.get("rfi_items") or a.get("_gate_rfis") or
                    a.get("rfis") or [])) or
      any("factory-finish" in str(n).lower()
          for n in a.get("notes", [])),
      "no RFI/note records the factory-finish exclusion")

# No negative token → all keys keep (positive evidence path unchanged).
_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
clean = _fishkill_analysis()
clean["exterior"]["notes"] = "Paint all AZEK trim and cornice per keynote."
clean["exterior"]["paint_evidence"] = "PAINT ALL FIBER CEMENT SIDING SW7674"
a = T._enforce_exterior_evidence(clean)
check(a["exterior"]["hardie_siding_sqft"] == 14800,
      f"hardie zeroed without negative evidence: "
      f"{a['exterior']['hardie_siding_sqft']}")

# Flag off → untouched.
_clear()
a = T._enforce_exterior_evidence(_fishkill_analysis())
check(a["exterior"]["hardie_siding_sqft"] == 14800,
      "gate ran with flag off")

print("— FIX A2: per-item positive evidence —")
# Honey Farms word-order case: "siding painted PT01" (material BEFORE the
# paint token) must keep the siding the old regex missed.
_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
honey = {
    "exterior": {"hardie_siding_sqft": 3200, "soffit_sqft": 280,
                 "window_trim_lf": 120,
                 "notes": "Primary cladding is James Hardie V-Groove fiber "
                          "cement siding painted PT01 (Benjamin Moore "
                          "OC-117 Simply White). Soffit panels painted to "
                          "match.",
                 "paint_evidence": None},
    "aggregated_totals": {},
}
a = T._enforce_exterior_evidence(honey)
check(a["exterior"]["hardie_siding_sqft"] == 3200,
      f"Honey 'siding painted PT01' siding zeroed: "
      f"{a['exterior']['hardie_siding_sqft']}")
check(a["exterior"]["soffit_sqft"] == 280,
      f"Honey painted soffit zeroed: {a['exterior']['soffit_sqft']}")
check(a["exterior"]["window_trim_lf"] == 0,
      f"unevidenced window trim survived per-item mode: "
      f"{a['exterior']['window_trim_lf']}")

# Dutchess case: painting callouts exist for OTHER items only — the
# unevidenced siding must not ride along (job-level gate kept it, $27k).
_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
dutch = {
    "exterior": {"hardie_siding_sqft": 5189, "azek_trim_lf": 480,
                 "notes": "Painted AZEK trim at entries per keynote 7. "
                          "Hardie panel siding per elevations.",
                 "paint_evidence": "'Painted AZEK trim' keynote 7"},
    "aggregated_totals": {},
}
a = T._enforce_exterior_evidence(dutch)
check(a["exterior"]["azek_trim_lf"] == 480,
      f"Dutchess painted AZEK zeroed: {a['exterior']['azek_trim_lf']}")
check(a["exterior"]["hardie_siding_sqft"] == 0,
      f"Dutchess unevidenced siding kept by ride-along: "
      f"{a['exterior']['hardie_siding_sqft']}")

# A negative sentence must never grant a positive: "siding ... not
# field-painted" names the material next to a paint token.
_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
neg = {
    "exterior": {"hardie_siding_sqft": 9000, "azek_trim_lf": 100,
                 "notes": "HardiePanel siding is factory-finished, not "
                          "field-painted. Painted AZEK trim throughout.",
                 "paint_evidence": None},
    "aggregated_totals": {},
}
a = T._enforce_exterior_evidence(neg)
check(a["exterior"]["hardie_siding_sqft"] == 0,
      f"negative sentence granted positive evidence: "
      f"{a['exterior']['hardie_siding_sqft']}")
check(a["exterior"]["azek_trim_lf"] == 100,
      f"AZEK lost in negative case: {a['exterior']['azek_trim_lf']}")

print("— FIX B: documented scope removal —")
_clear()
adj = {"category": "Ext. Hardie Siding", "from_value": 9427, "to_value": 0,
       "reason": "HardiePanel is a factory-finished fiber cement product "
                 "not field-painted per exterior notes."}
check(not W._is_documented_scope_removal(adj), "matched with flag off")
os.environ["NIGHTSHIFT_WILL_SCOPE_REMOVAL"] = "1"
check(W._is_documented_scope_removal(adj), "documented removal rejected")
check(not W._is_documented_scope_removal(
    {**adj, "to_value": 100}), "partial reduction wrongly allowed")
check(not W._is_documented_scope_removal(
    {**adj, "category": "Gyp. Walls"}), "interior category wrongly allowed")
check(not W._is_documented_scope_removal(
    {**adj, "reason": "seems high vs typical hotels"}),
      "undocumented reason wrongly allowed")
check(W._is_documented_scope_removal(
    {**adj, "reason": "Exterior repaint is by others per GC scope matrix"}),
      "by-others reason rejected")

print("— FIX C: elevation sheet guard —")
# Real sets WITH elevation sheets keep their pages under the guard.
for label, path in (("Fishkill", FISHKILL_PDF), ("Caris", CARIS_PDF)):
    _clear()
    if not os.path.exists(path):
        print(f"  … SKIP {label} PDF (not present)")
        continue
    pages_off = T._identify_elevation_pages(path)
    os.environ["NIGHTSHIFT_ELEV_REQUIRE_SHEETS"] = "1"
    pages_on = T._identify_elevation_pages(path)
    check(pages_on == pages_off and pages_on,
          f"{label} real elevations lost under guard: "
          f"off={pages_off} on={pages_on}")

# A set with NO elevation drawings — cue words only in general notes
# (the BofA class) — must abstain under the guard.
_clear()
import fitz  # noqa: E402
synth = os.path.join(HERE, ".synth_no_elev.pdf")
d = fitz.open()
p1 = d.new_page()
p1.insert_text((72, 72), "A-101 FIRST FLOOR PLAN")
p1.insert_text((72, 100),
               "GC NOTE: SEE BUILDING ELEVATION DRAWINGS (NOT IN SET) FOR "
               "CLADDING. PRIMARY CLADDING IS FIBER CEMENT SIDING.")
p2 = d.new_page()
p2.insert_text((72, 72), "A-102 SECOND FLOOR PLAN")
p2.insert_text((72, 100), "EXTERIOR ELEVATIONS BY OTHERS.")
d.save(synth)
d.close()
pages_off = T._identify_elevation_pages(synth)
os.environ["NIGHTSHIFT_ELEV_REQUIRE_SHEETS"] = "1"
pages_on = T._identify_elevation_pages(synth)
check(pages_off, "synthetic control invalid: cue pages not even selected "
                 "flag-off (test setup problem)")
check(pages_on == [],
      f"elevation-less set not abstained under guard: {pages_on}")
os.remove(synth)
_clear()

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
