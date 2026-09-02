#!/usr/bin/env python3
"""Elevation-pass evidence must survive the merge into the analysis.

Found 2026-09-01 by a prod-posture smoke test on Caris. The merge in
_maybe_run_exterior_pass copies a WHITELIST of numeric fields, so every
non-numeric key the elevation pass produced was dropped — including:

  - paint_evidence: the FIRST blob _enforce_exterior_evidence reads.
    The gate has been resolving exterior scope from `notes` alone on
    every job that runs the dedicated pass.
  - deterministic_text_evidence: the text-layer scan added specifically
    to feed that gate deterministically. It never reached it, so the
    Caris improvement previously credited to it was draw luck.

Live proof: the pass measured 2,922 SF of hardie, the mandate never
reached the gate, and the siding was zeroed.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


PASS_RESULT = {
    "exterior_paint_sqft": 617.0,
    "hardie_siding_sqft": 2922.0,
    "soffit_sqft": 90.0,
    "notes": "South and east elevations, fiber cement siding.",
    "paint_evidence": "KEYNOTE 7 — PAINT ALL HARDIE SIDING PT-1",
    "deterministic_text_evidence": {
        "paint_mandates": ["HARDIE SIDING PAINTED PT-1"],
        "factory_finish_notes": ["PREFINISHED METAL COPING"],
        "pages": [2, 3]},
    "_elev_consensus": {"draws": 3, "mode": "median"},
    "source_pages": [2, 3, 4],
}


def _run_merge(existing_ext=None):
    """Drive the real merge block via _maybe_run_exterior_pass with the
    pass stubbed out, so we exercise production code, not a copy."""
    analysis = {
        "project_info": {"building_type": "commercial"},
        "exterior": dict(existing_ext or {}),
        "floors": [], "notes": [],
    }
    orig = T._extract_exterior_scope_consensus
    T._extract_exterior_scope_consensus = lambda c, p: dict(PASS_RESULT)
    try:
        T._maybe_run_exterior_pass(None, "plans.pdf", analysis)
    finally:
        T._extract_exterior_scope_consensus = orig
    return analysis


print("1) Evidence fields survive the merge")
a = _run_merge()
ext = a.get("exterior") or {}
check(ext.get("paint_evidence") == PASS_RESULT["paint_evidence"],
      f"paint_evidence must survive: {ext.get('paint_evidence')!r}")
check(isinstance(ext.get("deterministic_text_evidence"), dict),
      f"deterministic scan must survive: "
      f"{ext.get('deterministic_text_evidence')!r}")
check(ext.get("_elev_consensus", {}).get("draws") == 3,
      "consensus metadata survives (provenance)")

print("\n2) Quantities still merge as before")
check(ext.get("hardie_siding_sqft") == 2922.0
      and ext.get("exterior_paint_sqft") == 617.0,
      f"numeric merge unchanged: {ext}")
check("fiber cement" in str(ext.get("notes")),
      "notes still appended")

print("\n3) Existing values are not clobbered")
a = _run_merge({"paint_evidence": "EARLIER EVIDENCE",
                "hardie_siding_sqft": 1000.0})
ext = a.get("exterior") or {}
check(ext.get("paint_evidence") == "EARLIER EVIDENCE",
      f"prior evidence wins: {ext.get('paint_evidence')!r}")
check(ext.get("hardie_siding_sqft") == 1000.0,
      "prior quantity wins (fill-gaps-only semantics)")

print("\n4) The gate can now read the mandate end-to-end")
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
os.environ.pop("NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE", None)
a = _run_merge()
a2 = T._enforce_exterior_evidence(a)
rec = a2.get("_exterior_evidence_gate") or {}
check(rec.get("evidence") is True,
      f"gate must find the paint mandate: {rec}")
check(a2["exterior"].get("hardie_siding_sqft") == 2922.0,
      f"evidenced siding survives pricing: "
      f"{a2['exterior'].get('hardie_siding_sqft')}")
os.environ.pop("NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE", None)

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ elevation evidence merge checks passed")
