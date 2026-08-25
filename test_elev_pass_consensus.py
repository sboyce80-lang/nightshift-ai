#!/usr/bin/env python3
"""Exterior-pass N-draw consensus (NIGHTSHIFT_ELEV_PASS_CONSENSUS):
empty draws are discarded unless all are empty; numeric fields take the
median of survivors; text/evidence rides the draw nearest the median
total. Overnight board 2026-08-25: Fishkill +7.0% -> -29.2% purely on an
empty-exterior draw."""
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


DRAWS = []


def fake_extract(client, pdf_path):
    return dict(DRAWS.pop(0)) if DRAWS else None


orig = T._extract_exterior_scope
T._extract_exterior_scope = fake_extract

full_a = {"hardie_siding_sqft": 5200, "azek_trim_lf": 600,
          "exterior_door_count": 4, "notes": "draw A",
          "paint_evidence": "PAINT SIDING",
          "siding_class_sqft": {"v_groove": 2600}}
full_b = {"hardie_siding_sqft": 6000, "azek_trim_lf": 660,
          "exterior_door_count": 4, "notes": "draw B",
          "paint_evidence": "PAINT SIDING"}
empty = {"hardie_siding_sqft": 0, "azek_trim_lf": 0,
         "exterior_door_count": 0, "notes": "storefront only",
         "paint_evidence": "NONE"}

try:
    # n=1 legacy passthrough
    os.environ["NIGHTSHIFT_ELEV_PASS_CONSENSUS"] = "1"
    DRAWS[:] = [dict(full_a)]
    out = T._extract_exterior_scope_consensus(None, "x.pdf")
    check(out["notes"] == "draw A" and "_elev_consensus" not in out,
          "n=1 not a passthrough")

    # empty draw discarded; medians across the two full draws
    os.environ["NIGHTSHIFT_ELEV_PASS_CONSENSUS"] = "3"
    DRAWS[:] = [dict(full_a), dict(empty), dict(full_b)]
    out = T._extract_exterior_scope_consensus(None, "x.pdf")
    check(out["hardie_siding_sqft"] == 5600.0,
          f"median wrong: {out['hardie_siding_sqft']}")
    check(out["azek_trim_lf"] == 630.0,
          f"azek median wrong: {out['azek_trim_lf']}")
    check(out["_elev_consensus"]["non_empty"] == 2,
          f"empty draw kept: {out['_elev_consensus']}")
    check(out.get("paint_evidence") == "PAINT SIDING",
          "evidence lost in consensus")
    # class share scaled to the median siding total (2600 * 5600/5200)
    cls = out.get("siding_class_sqft") or {}
    check(not cls or abs(cls.get("v_groove", 0) - 2800.0) < 0.6,
          f"class share not scaled: {cls}")

    # all-empty draws survive as an honest empty result
    DRAWS[:] = [dict(empty), dict(empty), dict(empty)]
    out = T._extract_exterior_scope_consensus(None, "x.pdf")
    check(out["hardie_siding_sqft"] == 0,
          "all-empty consensus invented scope")

    # total failure -> None
    DRAWS[:] = []
    out = T._extract_exterior_scope_consensus(None, "x.pdf")
    check(out is None, "no-draw case not None")
finally:
    T._extract_exterior_scope = orig
    os.environ.pop("NIGHTSHIFT_ELEV_PASS_CONSENSUS", None)

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
