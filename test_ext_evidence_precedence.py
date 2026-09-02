#!/usr/bin/env python3
"""An "unconfirmed" spec note must not outrank "FIELD PAINT" on the schedule.

2026-09-02, 168 Holley St. A-301's Exterior Finish Schedule reads
"LAP SIDING - PRIMED FOR PAINT / FIELD PAINT - DARK HUNTER GREEN (SW0041)";
the words ColorPlus and FACTORY appear zero times on that sheet, while
PRIMED FOR and FIELD PAINT appear four times each. The gate still zeroed
2,832 SF of Hardie siding, citing "Hardie siding noted as factory finish
(ColorPlus or equivalent) status unconfirmed" — a note about the A-000
SPECIFICATION sheet, where it is a true statement that the finish is not
stated.

Two defects, both proven against the real run:

  1. POLARITY. _EXT_FACTORY_FINISH_RX matched the bare token "factory
     finish" inside a sentence declaring the status UNKNOWN, reading an
     absence of evidence as an affirmative factory-finish claim.
  2. PRECEDENCE. Negatives were applied first and the key popped from
     `present`, so the positive pass never evaluated it. Soffit survived
     only because it carried no negative note — and was then kept on
     evidence from the same schedule table four rows down.

A third hazard surfaced while testing: the gate writes its verdict into
analysis["notes"] ("[Exterior Evidence] Zeroed factory-finished item(s):
hardie_siding_sqft=2,832"), and that sentence both matches the
factory-finish pattern and names the material family. Re-entering the gate
on a stored analysis makes it re-zero on its own output.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDE_API_KEY", "x")
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


def load(flag):
    os.environ["NIGHTSHIFT_EXT_EVIDENCE_PRECEDENCE"] = flag
    sys.modules.pop("Takeoff_DIRECT", None)
    import Takeoff_DIRECT as T
    return T


T = load("1")

HEDGED = "Hardie siding noted as factory finish (ColorPlus or equivalent) status unconfirmed"
REAL = "All Hardie siding is factory finished and shall not be field-painted"
POSITIVE = ("Exterior Finish Schedule (Sheet A-301): Tag 1 LAP SIDING - "
            "'PRIMED FOR PAINT / FIELD PAINT - DARK HUNTER GREEN (SW0041)'")
SELF_NOTE = ("[Exterior Evidence] Zeroed factory-finished item(s): "
             "exterior_paint_sqft=120, hardie_siding_sqft=2,832")

# --- 1) Polarity: hedged is not a claim; unhedged still is.
check(not T._ext_finish_sentence_is_negative(HEDGED),
      "a hedged 'status unconfirmed' note still read as factory finish")
check(T._ext_finish_sentence_is_negative(REAL),
      "a genuine factory-finish declaration stopped registering")
for hedge in ("status unknown", "not explicitly stated", "unclear",
              "to be confirmed", "TBD", "not specified"):
    s = f"Hardie siding factory finish {hedge} on this sheet"
    check(not T._ext_finish_sentence_is_negative(s),
          f"hedge {hedge!r} not recognised")

# --- 2) The gate never treats its own note as drawing evidence.
check(not T._ext_finish_sentence_is_negative(SELF_NOTE),
      "the gate's own verdict note read back as fresh evidence")

# --- 3) An explicit positive callout outranks a factory-finish note.
keys = ["hardie_siding_sqft", "exterior_paint_sqft"]
neg = T._ext_negative_evidence_keys(keys, [REAL, POSITIVE])
pos = T._ext_positive_evidence_keys(keys, [REAL, POSITIVE])
check("hardie_siding_sqft" in pos,
      "explicit FIELD PAINT schedule row yielded no positive evidence")
check("hardie_siding_sqft" in neg,
      "fixture drift: the genuine negative should still be detected")

def _gate(blob_notes, qty=2832):
    # The gate reads exterior.paint_evidence / exterior.notes — NOT
    # analysis["notes"]. Putting the text in the wrong bag makes every
    # quantity fall through to the no-mandate pricing-tier zeroing, which
    # silently turns this whole test green for the wrong reason.
    a = {"exterior": {"hardie_siding_sqft": qty, "soffit_sqft": 192,
                      "notes": " | ".join(blob_notes)},
         "notes": [], "aggregated_totals": {},
         "project_info": {"building_type": "commercial"}, "rfi_items": []}
    T._enforce_exterior_evidence(a)
    return a

kept = _gate([HEDGED, POSITIVE])
check(kept["exterior"]["hardie_siding_sqft"] == 2832,
      f"siding zeroed despite an explicit paint callout: "
      f"{kept['exterior']['hardie_siding_sqft']}")

# A genuine, unhedged negative with NO positive still zeroes — the gate must
# not become toothless.
zeroed = _gate([REAL])
check(zeroed["exterior"]["hardie_siding_sqft"] == 0,
      "a genuine factory-finish note no longer zeroes")

# --- 4) Flag OFF reproduces the old (wrong) behaviour exactly.
T = load("0")
old = _gate([HEDGED, POSITIVE])
check(old["exterior"]["hardie_siding_sqft"] == 0,
      "kill switch did not restore the pre-fix behaviour")
check(T._ext_finish_sentence_is_negative(HEDGED),
      "hedge-awareness leaked through with the flag OFF")

sys.modules.pop("Takeoff_DIRECT", None)
os.environ.pop("NIGHTSHIFT_EXT_EVIDENCE_PRECEDENCE", None)
import Takeoff_DIRECT as T
check(T._ext_evidence_precedence_enabled(), "precedence fix is not ON by default")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
