#!/usr/bin/env python3
"""F3 (2026-08-20 JW batch): unit-mix coverage gate tests.

Hudson shape: 150-key hotel, typicals multiplied by units drawn (1-3)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _an(total_units, rooms):
    return {"project_info": {"total_units": total_units},
            "floors": [{"floor_name": "1", "rooms": rooms}],
            "notes": []}


def _unit_room(ut, mult, name="Living"):
    return {"room_id": f"{ut}-{name}", "room_name": name, "in_scope": True,
            "unit_type": ut, "unit_multiplier": mult,
            "dimensions": {}, "elements": {}}


class TestUnitMixGate(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_UNIT_MIX_GATE", None)

    def test_hudson_shape_flags_under(self):
        # 150 keys; unit types A/B/C multiplied 1-3 each
        a = _an(150, [_unit_room("A", 1), _unit_room("B", 3),
                      _unit_room("C", 2)])
        out = td._enforce_unit_mix_coverage(a)
        rec = out["_unit_mix_gate"]
        self.assertTrue(rec["flagged"])
        self.assertEqual(rec["covered_instances"], 6)
        self.assertTrue(out.get("manual_review_required"))
        self.assertIn("UNDER", " ".join(out["notes"]))

    def test_multifamily_over_flags(self):
        # inventory says 8 units; extraction multiplied 24 instances
        a = _an(8, [_unit_room("S1", 24)])
        out = td._enforce_unit_mix_coverage(a)
        self.assertTrue(out["_unit_mix_gate"]["flagged"])
        self.assertIn("OVER", " ".join(out["notes"]))

    def test_covered_project_passes(self):
        a = _an(13, [_unit_room("S1", 8), _unit_room("S2", 5)])
        out = td._enforce_unit_mix_coverage(a)
        self.assertFalse(out["_unit_mix_gate"]["flagged"])
        self.assertFalse(out.get("manual_review_required"))

    def test_max_not_sum_within_type(self):
        # 3 rooms of the same unit type share one multiplier — not 3x
        rooms = [_unit_room("A", 10, "Living"), _unit_room("A", 10, "Bed"),
                 _unit_room("A", 10, "Bath")]
        out = td._enforce_unit_mix_coverage(_an(10, rooms))
        self.assertEqual(out["_unit_mix_gate"]["covered_instances"], 10)
        self.assertFalse(out["_unit_mix_gate"]["flagged"])

    def test_no_unit_typicals_is_noop(self):
        a = _an(150, [{"room_id": "101", "room_name": "Lobby",
                       "in_scope": True, "dimensions": {}, "elements": {}}])
        out = td._enforce_unit_mix_coverage(a)
        self.assertFalse(out["_unit_mix_gate"]["flagged"])

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "0"
        a = _an(150, [_unit_room("A", 1)])
        out = td._enforce_unit_mix_coverage(a)
        self.assertNotIn("_unit_mix_gate", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
