#!/usr/bin/env python3
"""Power-wash allowance line must not print as a bare "Additional scope" row.

2026-08-22: Hudson Hotel's "Power Washing (ALLOWANCE — per plans note)"
$38,151 line matched no estimate bucket and rendered as an unexplained
"Additional scope" amount (the Biddle specialty-line failure class,
2026-07-21). Locks in: (1) power-wash lines get their own labeled bucket,
(2) any residual unmatched line prints its cost-line labels as scope text.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_estimate_pdf as G

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def _result(items):
    return {"cost_estimate": {"line_items": items}}


# 1) Power-wash allowance line lands in its own bucket, not misc.
pw = [{"item": "Power Washing (ALLOWANCE — per plans note) - 24,652 sqft @ $1.46",
       "qty": 24652, "total": 38151.44}]
rows = G._build_line_items(_result(pw))
check(rows and rows[0]["title"] == "Power washing",
      f"power-wash line not in Power washing bucket: {rows}")
check(rows and "allowance" in rows[0]["scope"].lower(),
      f"power-wash scope missing allowance language: {rows}")

# 2) "Pressure Wash" variant routes the same way.
rows = G._build_line_items(_result(
    [{"item": "Pressure Wash Exterior Decks - 900 sqft @ $1.46",
      "qty": 900, "total": 1314.0}]))
check(rows and rows[0]["title"] == "Power washing",
      f"pressure-wash variant not bucketed: {rows}")

# 3) A genuinely unmatched line still prints, but never as a bare amount —
#    its cost-line label becomes the scope text.
mystery = [{"item": "Fresco Restoration - 12 sqft @ $99.00",
            "qty": 12, "total": 1188.0}]
rows = G._build_line_items(_result(mystery))
check(rows and rows[-1]["title"] == "Additional scope",
      f"unmatched line lost: {rows}")
check(rows and "Fresco Restoration" in rows[-1]["scope"],
      f"Additional scope row has no explanatory labels: {rows}")

# 4) Regression guard: the pre-existing buckets are unchanged for a plain
#    interior line and power wash never swallows exterior paint lines.
mixed = [
    {"item": "Interior Walls (GYP) - 10,000 sqft @ $1.00", "qty": 10000,
     "total": 10600.0},
    {"item": "Ext. Siding Paint - 2,000 sqft @ $2.00", "qty": 2000,
     "total": 4240.0},
    {"item": "Power Washing (ALLOWANCE — per plans note) - 24,652 sqft @ $1.46",
     "qty": 24652, "total": 38151.44},
]
rows = G._build_line_items(_result(mixed))
titles = [r["title"] for r in rows]
check(titles == ["Interior painting — walls & ceilings", "Exterior",
                 "Power washing"],
      f"mixed bucketing order/content wrong: {titles}")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
