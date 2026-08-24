#!/usr/bin/env python3
"""Sales-Floor-ACT evidence guard (NIGHTSHIFT_SALES_FLOOR_ACT_EVIDENCE):
the ACT→DRYFALL auto-flip requires painted-deck document evidence; an
'EXP.' note or silence downgrades to RFI-only. TSC Fusion 2026-08-24:
the assumption fabricated $16.9k of dryfall on an exposed-unpainted
sales floor (Rider takeoff: 0 SF ceilings)."""
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


ev = T._act_flip_has_evidence
check(not ev("Ceiling noted as 'EXP. SHOWROOM' on RCP; ACT elsewhere"),
      "TSC 'EXP.' note treated as paint evidence")
check(not ev("Sales floor ceiling ACT per RCP"),
      "silence treated as paint evidence")
check(ev("General note: PAINT ALL EXPOSED STRUCTURE TO DECK, DRYFALL"),
      "explicit dryfall callout rejected")
check(ev("Ceiling: open structure, painted deck per spec 09 91 00"),
      "painted deck spec rejected")
check(not ev(""), "empty blob treated as evidence")
check(not ev("exposed structure unpainted per RCP legend"),
      "exposed-unpainted treated as evidence")

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
