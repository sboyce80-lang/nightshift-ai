#!/usr/bin/env python3
"""Degenerate-read guard (always-on F1 hardening): a plan-sheet read
returning rooms with NO dimensions retries once and is never
checkpointed. Honey 103pp (2026-08-24): a slim-retry draw produced 10
dimension-less rooms / 0 SF walls, checkpointed the empties, and every
replay inherited them."""
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


deg = T._sheet_read_is_degenerate

# Rooms without any dimension → degenerate.
check(deg({"floors": [{"rooms": [
    {"room_name": "BOH Storage", "dimensions": {}},
    {"room_name": "Office", "dimensions": {"wall_area_sqft": 0}}]}]}),
    "dimension-less rooms not flagged")

# One room with real dims → healthy.
check(not deg({"floors": [{"rooms": [
    {"room_name": "BOH", "dimensions": {}},
    {"room_name": "Sales", "dimensions": {"wall_area_sqft": 2871}}]}]}),
    "healthy read flagged")

# Zero rooms with an explanation → legitimate (site/refrig sheets).
check(not deg({"floors": [], "notes": ["site lighting only"]}),
      "zero-room sheet flagged")
check(not deg({"floors": [{"rooms": []}]}), "empty floors flagged")

# Alternate dim keys count as dims.
check(not deg({"floors": [{"rooms": [
    {"room_name": "Corr", "dimensions": {"length_ft": 40,
                                         "width_ft": 6}}]}]}),
    "length/width dims not recognized")

# Non-dict input safe.
check(not deg(None), "None crashed or flagged")

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
