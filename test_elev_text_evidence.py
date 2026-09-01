#!/usr/bin/env python3
"""Deterministic elevation text evidence (NIGHTSHIFT_ELEV_TEXT_EVIDENCE).

Caris r2 (2026-08-28): the elevation pass measured exterior in ALL 5
draws but the evidence gate's verdict split 3 ways on whether that
draw's LLM read transcribed the factory-finish note — the 2 draws that
did were both in band (+3.0/+9.5); the 3 that didn't ran −16/−18. The
note is in the PDF text layer; reading it must not involve dice.
Locks in: flag gate, regex classes, gate-blob integration, and the
live Caris scan (PREFINISHED found deterministically)."""
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


print("1) Regex classes")
p = T._DET_EXT_PAINT_RX
f = T._DET_EXT_FACTORY_RX
check(bool(p.search("HARDIE SIDING PAINTED PT-1 TYP")),
      "siding+paint phrase is a mandate")
check(bool(p.search("field paint all exterior trim")),
      "field-paint phrase is a mandate")
check(bool(p.search("PT-3 AT ALL FASCIA")), "PT-x near fascia is a mandate")
check(not p.search("painted metal roof by manufacturer"),
      "paint without a tracked material is not a mandate")
check(bool(f.search("PREFINISHED METAL COPING")), "prefinished detected")
check(bool(f.search("factory finish siding")), "factory finish detected")
check(bool(f.search("finish by manufacturer")), "by-manufacturer detected")
check(not f.search("smooth finish paint"), "plain finish is not factory")

print("\n2) Flag off -> None; no text layer -> marked")
os.environ.pop("NIGHTSHIFT_ELEV_TEXT_EVIDENCE", None)
check(T._deterministic_elev_text_evidence("x.pdf", [0]) is None,
      "flag off must return None")
os.environ["NIGHTSHIFT_ELEV_TEXT_EVIDENCE"] = "1"
check(T._deterministic_elev_text_evidence("/nonexistent.pdf", [0])
      is None, "unreadable pdf is non-fatal None")

print("\n3) Live Caris scan is deterministic (when plans available)")
caris = os.path.join(os.path.dirname(HERE), "nightshift-repo",
                     "nsai_batch_2026-08-20", "caris_hyde_park",
                     "plans_clean.pdf")
if os.path.exists(caris):
    a = T._deterministic_elev_text_evidence(caris, [1, 2, 3])
    b = T._deterministic_elev_text_evidence(caris, [1, 2, 3])
    check(a == b, "two scans must be identical")
    check(any("PREFINISHED" in h.upper()
              for h in a.get("factory_finish_notes", [])),
          f"Caris factory-finish note found: {a}")
else:
    print("  (Caris plans not present — live scan skipped)")

print("\n4) Gate blobs include the deterministic hits")
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
os.environ["NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE"] = "1"
analysis = {
    "exterior": {
        "exterior_paint_sqft": 800.0,
        "hardie_siding_sqft": 3100.0,
        "notes": "",
        "paint_evidence": "",
        "deterministic_text_evidence": {
            "paint_mandates": [],
            "factory_finish_notes": ["PREFINISHED HARDIE SIDING"],
            "pages": [2, 3]},
    },
    "notes": [],
}
a = T._enforce_exterior_evidence(analysis)
ffa = a["exterior"].get("_factory_finish_allowance") or {}
check("hardie_siding_sqft" in ffa,
      f"deterministic factory note must route siding to allowance: "
      f"{a['exterior'].get('_factory_finish_allowance')}, "
      f"gate={a.get('_exterior_evidence_gate')}")
check(a["exterior"]["hardie_siding_sqft"] == 3100.0,
      "allowance path must keep the measured quantity")

os.environ.pop("NIGHTSHIFT_ELEV_TEXT_EVIDENCE", None)
os.environ.pop("NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE", None)
os.environ.pop("NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE", None)
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all deterministic evidence checks passed")
