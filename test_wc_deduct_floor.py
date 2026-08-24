#!/usr/bin/env python3
"""WC deduct floor (NIGHTSHIFT_WC_DEDUCT_FLOOR): after the WC wall
deduction, at least wc*(1-s)/s of painted wall survives under the mixed
share s — the schedule that designated the WC also designated the PT
remainder. Homewood round-2: 95.6k WC >= extracted walls clamped painted
gyp to $0 (-$238k)."""
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


def walls_line(costs):
    for li in costs["line_items"]:
        s = str(li.get("item"))
        if s.startswith("Gyp. Walls"):
            return s, float(li.get("total") or 0)
    return "", 0.0


def run(walls, wc, floor_on, share="0.8"):
    for k in ("NIGHTSHIFT_WC_WALL_DEDUCT", "NIGHTSHIFT_WC_DEDUCT_FLOOR",
              "NIGHTSHIFT_WC_MIXED_SHARE"):
        os.environ.pop(k, None)
    os.environ["NIGHTSHIFT_WC_WALL_DEDUCT"] = "1"
    os.environ["NIGHTSHIFT_WC_MIXED_SHARE"] = share
    if floor_on:
        os.environ["NIGHTSHIFT_WC_DEDUCT_FLOOR"] = "1"
    return T.calculate_costs(
        {"total_paintable_wall_sqft": walls,
         "total_wallcovering_sqft": wc},
        exterior={}, building_type="hospitality")


# Homewood shape: WC exceeds walls; flag off -> clamp to 0 (legacy).
s, tot = walls_line(run(90000, 95554, floor_on=False))
check("0 sqft" in s, f"legacy clamp changed: {s}")

# Flag on -> painted remainder floor = 95,554 * 0.25 = 23,888.5.
s, tot = walls_line(run(90000, 95554, floor_on=True))
check("23,888" in s or "23,889" in s, f"floor not applied: {s}")
check(tot > 15000, f"floored walls priced trivially: {tot}")

# Deduct leaving MORE than the floor is untouched.
s, _ = walls_line(run(200000, 95554, floor_on=True))
check("104,446" in s, f"normal deduct altered: {s}")

# Floor never exceeds pre-deduct walls.
s, _ = walls_line(run(10000, 95554, floor_on=True))
check("10,000" in s, f"floor exceeded extracted walls: {s}")

# Share unset -> floor inert even when flag on.
s, _ = walls_line(run(90000, 95554, floor_on=True, share="0"))
check("0 sqft" in s, f"floor fired without a share: {s}")

for k in ("NIGHTSHIFT_WC_WALL_DEDUCT", "NIGHTSHIFT_WC_DEDUCT_FLOOR",
          "NIGHTSHIFT_WC_MIXED_SHARE"):
    os.environ.pop(k, None)

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
