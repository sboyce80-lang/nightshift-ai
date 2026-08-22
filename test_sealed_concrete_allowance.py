#!/usr/bin/env python3
"""S6 (2026-08-21): sealed-concrete allowance tests."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _room(name, area, notes="", mats=None, conc=0):
    return {"room_id": name, "room_name": name, "in_scope": True,
            "notes": notes, "materials": mats or {},
            "dimensions": {"floor_area_sqft": area},
            "elements": {"concrete_floor_sqft": conc}}


def _an(rooms):
    return {"floors": [{"floor_name": "G", "rooms": rooms}],
            "aggregated_totals": {"total_concrete_floor_sqft": 0},
            "notes": []}


class TestSealedConcreteAllowance(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_SEALED_CONCRETE_ALLOWANCE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_SEALED_CONCRETE_ALLOWANCE", None)

    def test_utility_rooms_get_allowance(self):
        a = _an([_room("Basement", 4000), _room("Mechanical 005", 800),
                 _room("Open Office", 900)])
        out = td._apply_sealed_concrete_allowance(a)
        rooms = out["floors"][0]["rooms"]
        self.assertEqual(rooms[0]["elements"]["concrete_floor_sqft"], 4000)
        self.assertEqual(rooms[1]["elements"]["concrete_floor_sqft"], 800)
        self.assertEqual(rooms[2]["elements"]["concrete_floor_sqft"], 0)
        self.assertEqual(out["aggregated_totals"]
                         ["total_concrete_floor_sqft"], 4800)
        self.assertTrue(any("Sealed-Concrete Allowance" in str(n)
                            for n in out["notes"]))
        self.assertTrue(any("Sealed Concrete" == r.get("category")
                            for r in (out.get("_pre_pricing_rfis") or [])))

    def test_documented_finish_skipped(self):
        a = _an([_room("Storage", 500, notes="Floor: VCT per schedule"),
                 _room("Basement", 600,
                       mats={"floor": "Polished Concrete PC-1"})])
        out = td._apply_sealed_concrete_allowance(a)
        for r in out["floors"][0]["rooms"]:
            self.assertEqual(r["elements"]["concrete_floor_sqft"], 0)

    def test_existing_quantity_untouched(self):
        a = _an([_room("Basement", 4000, conc=3500)])
        out = td._apply_sealed_concrete_allowance(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 3500)

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_SEALED_CONCRETE_ALLOWANCE"] = "0"
        a = _an([_room("Basement", 4000)])
        out = td._apply_sealed_concrete_allowance(a)
        self.assertNotIn("_sealed_concrete_allowance", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
