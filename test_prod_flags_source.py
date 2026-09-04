#!/usr/bin/env python3
"""The production flag set has one definition, and it holds no secrets.

2026-09-04: a full 8-job golden replay, a re-run of it, and a 5-point bisect
all produced unusable numbers because each harness carried its own FLAGS
dict — run_one.py 9, rerun_batch.sh 29, the replay 38 — against a production
that runs 64. Two of the missing ones were decisive: PER_SHEET_CONSENSUS=3
(prod reads every sheet three times) and VME_STARVED_PROMOTE=1 (credited in
RERUN_RESULTS with lifting Harlem's walls 1,558 -> 13,213 SF).

Locks in: (1) the file parses and is non-trivial, (2) it never carries a
credential, (3) applying it actually populates the environment, (4) the
drift check catches a flag production does not define.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from golden.load_prod_flags import (read_prod_flags, apply_prod_flags,
                                    assert_no_extra_nightshift_flags,
                                    PROD_FLAGS_PATH)

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


flags = read_prod_flags()
check(len(flags) >= 50, f"prod flag set looks truncated: {len(flags)}")
check(all(k.startswith("NIGHTSHIFT_") for k in flags),
      "a non-NIGHTSHIFT key leaked into the flag set")

# No credentials, ever. The capture is a copy-paste from a dashboard that
# also shows DATABASE_URL right above the flags.
raw = open(PROD_FLAGS_PATH).read()
for needle in ("postgres://", "postgresql://", "SECRET", "PASSWORD",
               "_KEY=", "API_KEY", "@dpg-"):
    check(needle.lower() not in raw.lower(),
          f"credential-shaped string {needle!r} in the flag file")

# The two that mattered on 2026-09-04 must be present and correct.
check(flags.get("NIGHTSHIFT_PER_SHEET_CONSENSUS") == "3",
      f"per-sheet consensus wrong: {flags.get('NIGHTSHIFT_PER_SHEET_CONSENSUS')}")
check(flags.get("NIGHTSHIFT_VME_STARVED_PROMOTE") == "1",
      "starved-promote missing — this is what lifts Harlem's walls")
# Values matter as much as names: "0" and absent are different states.
check(flags.get("NIGHTSHIFT_CEILING_ASSUME_PAINTED") == "0",
      "an explicitly-OFF flag was captured as ON or dropped")

for k in ("NIGHTSHIFT_PER_SHEET_CONSENSUS", "NIGHTSHIFT_VME_STARVED_PROMOTE"):
    os.environ.pop(k, None)
applied = apply_prod_flags()
check(os.environ.get("NIGHTSHIFT_PER_SHEET_CONSENSUS") == "3",
      "apply_prod_flags did not populate the environment")
check(len(applied) == len(flags), "apply returned a different set than it read")

os.environ["NIGHTSHIFT_MADE_UP_FLAG"] = "1"
try:
    assert_no_extra_nightshift_flags()
    check(False, "drift check missed an undefined NIGHTSHIFT flag")
except AssertionError as e:
    check("NIGHTSHIFT_MADE_UP_FLAG" in str(e), "drift check named the wrong flag")
finally:
    os.environ.pop("NIGHTSHIFT_MADE_UP_FLAG", None)
check(assert_no_extra_nightshift_flags(), "drift check false-positives on a clean env")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
