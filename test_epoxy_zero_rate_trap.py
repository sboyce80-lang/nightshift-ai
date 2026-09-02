#!/usr/bin/env python3
"""Epoxy wall coating is wired to price at $0.00 — do not "fix" it halfway.

2026-09-02, 168 Holley St (Profeta Painting). The A-207 finish plan tags
PT-2/PT-3 Epoxy on all three toilet rooms, and General Finish Note J reads
"USE EPOXY PAINT ON WALLS IN ALL TOILET ROOMS", yet
total_epoxy_wall_sqft came out 0. That looks like an under-extraction bug.
It is not — it is the documented design. config.PRICING_MODEL marks epoxy
an "Operator-Activated Option": net-new scope lines default to $0 until a
contractor enables the line and enters their own rate, and the header there
states plainly that "the takeoff engine does not yet auto-extract
quantities for these — extraction wiring is a separate follow-up."

Three things therefore hold together:

  1. NIGHTSHIFT_EXTENDED_SCOPE is off (absent from the prod flag set and
     from render.yaml), so the epoxy branch in the aggregator is skipped.
  2. Nothing propagates a finish schedule's wall_finish into a room's
     materials.walls, so every restroom reads "GYP" regardless.
  3. config.PRICING_MODEL["epoxy_wall_area"] is rated 0.0 by default.

Because (1) and (2) hold, those ~1,107 SF fall through to the gyp/paintable
catch-all and bill at the wall rate — roughly right. Doing the extraction
follow-up WITHOUT first setting a rate would move that area onto a $0.00
line and LOWER the estimate by about $1,046. The ordering the config header
describes — operator sets a rate, then extraction is wired — is load-bearing.

This test fails the moment epoxy extraction is enabled while the rate is
still zero, so that ordering cannot be inverted quietly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDE_API_KEY", "x")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


import config

def _rate(key):
    tiers = (config.PRICING_MODEL.get(key) or {}).get("tiers") or []
    return max((float(t.get("rate") or 0) for t in tiers), default=0.0)


epoxy_rate = _rate("epoxy_wall_area")
wall_rate = _rate("gyp_walls")
print(f"  epoxy_wall_area max rate = ${epoxy_rate:.2f} | gyp_walls = ${wall_rate:.2f}")

import Takeoff_DIRECT as T
extraction_live = T._extended_scope_enabled()

# THE INVARIANT: epoxy area may only be routed to its own line once that
# line can actually bill. Either the rate is real, or extraction stays off.
check(epoxy_rate > 0 or not extraction_live,
      "epoxy extraction is ENABLED while epoxy_wall_area is rated $0.00 — "
      "epoxy area will bill at zero instead of falling through to the wall "
      "rate. Set the operator rate for 'epoxy_wall_area' before enabling "
      "NIGHTSHIFT_EXTENDED_SCOPE.")

# And if a rate ever is set, it should not be below the plain wall rate —
# epoxy is a more expensive system, never a discount.
if epoxy_rate > 0:
    check(epoxy_rate >= wall_rate,
          f"epoxy rate ${epoxy_rate:.2f} is below the gyp wall rate "
          f"${wall_rate:.2f}: routing walls to epoxy would cut the bid")

# Document the current state so a change is deliberate, not incidental.
check(not extraction_live,
      "NIGHTSHIFT_EXTENDED_SCOPE default changed: re-read this file's "
      "header before shipping — the epoxy rate must be set first")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
