#!/usr/bin/env python3
"""S2 (2026-08-21): door-source reconcile — Caris shape (93 → 68 vs JW 75)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _room(sheet, doors, name="R"):
    return {"room_id": name, "room_name": name, "in_scope": True,
            "source_sheet": sheet, "dimensions": {},
            "elements": {"doors_full_paint": doors}}


def _an(rooms, agg_doors):
    return {"floors": [{"floor_name": "1", "rooms": rooms}],
            "aggregated_totals": {"total_doors_full_paint": agg_doors},
            "notes": []}


class TestDoorSourceReconcile(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_DOOR_SOURCE_RECONCILE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_DOOR_SOURCE_RECONCILE", None)

    def test_caris_shape_keeps_larger_family(self):
        rooms = ([_room("A9.1", 34, "d1"), _room("A9.1", 34, "d2")]
                 + [_room("A1.2", 5, f"p{i}") for i in range(5)])
        out = td._reconcile_door_sources(_an(rooms, 93))
        self.assertEqual(out["aggregated_totals"]["total_doors_full_paint"],
                         68)
        self.assertEqual(out["_door_source_reconcile"]["kept"], "detail")
        self.assertTrue(all(r["elements"]["doors_full_paint"] == 0
                            for r in rooms[2:]))

    def test_plan_family_wins_when_larger(self):
        rooms = ([_room("A9.1", 20, "d1")]
                 + [_room("A1.1", 10, f"p{i}") for i in range(5)])
        out = td._reconcile_door_sources(_an(rooms, 70))
        self.assertEqual(out["_door_source_reconcile"]["kept"], "plan")
        self.assertEqual(out["aggregated_totals"]["total_doors_full_paint"],
                         50)

    def test_within_tolerance_untouched(self):
        rooms = [_room("A9.1", 60, "d1"), _room("A1.1", 8, "p1")]
        out = td._reconcile_door_sources(_an(rooms, 68))
        self.assertEqual(out["_door_source_reconcile"].get("noop"),
                         "within_tolerance")
        self.assertEqual(out["aggregated_totals"]["total_doors_full_paint"],
                         68)

    def test_schedule_authoritative_skips(self):
        rooms = [_room("A9.1", 60, "d1"), _room("A1.1", 30, "p1")]
        a = _an(rooms, 90)
        a["_schedule_authoritative_counts"] = {"total_doors_full_paint": 75}
        out = td._reconcile_door_sources(a)
        self.assertEqual(out["_door_source_reconcile"].get("noop"),
                         "schedule_authoritative")

    def test_single_family_noop(self):
        rooms = [_room("A1.1", 10, "p1"), _room("A1.2", 10, "p2")]
        out = td._reconcile_door_sources(_an(rooms, 20))
        self.assertEqual(out["_door_source_reconcile"].get("noop"),
                         "single_family")


if __name__ == "__main__":
    unittest.main(verbosity=2)
