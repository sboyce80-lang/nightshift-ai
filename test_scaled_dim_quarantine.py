#!/usr/bin/env python3
"""F6+F7 (2026-08-20 JW batch): scaled-dim quarantine + Level-5 allowance."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _an(rooms, wall_total):
    return {"floors": [{"floor_name": "1", "rooms": rooms}],
            "aggregated_totals": {"total_paintable_wall_sqft": wall_total},
            "notes": []}


def _room(name, wall, note):
    return {"room_id": name, "room_name": name, "in_scope": True,
            "notes": note,
            "dimensions": {"wall_area_sqft": wall, "perimeter_lf": 50},
            "elements": {}}


class TestScaledDimQuarantine(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_SCALED_DIM_QUARANTINE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_SCALED_DIM_QUARANTINE", None)

    def test_ulum_rcp_marker_quarantines_walls(self):
        a = _an([_room("Lounge", 800,
                       "All room dimensions are scaled from the RCP at "
                       "1/4\"=1'-0\" scale. These are approximate and must "
                       "be verified against dimensioned floor plan sheets."),
                 _room("Kitchen", 600, "Dimensioned on A-100: 18'-9\" x 38'")],
                1400)
        out = td._quarantine_scaled_dims(a)
        r0 = out["floors"][0]["rooms"][0]
        self.assertEqual(r0["dimensions"]["wall_area_sqft"], 0)
        self.assertEqual(r0["_scaled_dim_quarantined"]["wall_area_sqft"], 800)
        # dimensioned room untouched
        self.assertEqual(out["floors"][0]["rooms"][1]["dimensions"]
                         ["wall_area_sqft"], 600)
        self.assertEqual(out["aggregated_totals"]
                         ["total_paintable_wall_sqft"], 600)
        self.assertTrue(any("Scaled-Dim Quarantine" in str(n)
                            for n in out["notes"]))

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_SCALED_DIM_QUARANTINE"] = "0"
        a = _an([_room("Lounge", 800, "scaled from the RCP")], 800)
        out = td._quarantine_scaled_dims(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["dimensions"]
                         ["wall_area_sqft"], 800)

    def test_idempotent(self):
        a = _an([_room("Lounge", 800, "dimensions are approximate")], 800)
        once = td._quarantine_scaled_dims(a)
        n = len(once["notes"])
        twice = td._quarantine_scaled_dims(once)
        self.assertEqual(len(twice["notes"]), n)


class TestLevel5Allowance(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_LEVEL5_ALLOWANCE", None)

    def _l5(self, ce):
        return next(li for li in ce["line_items"]
                    if "Level 5" in str(li.get("item", "")))

    def test_default_label_unchanged(self):
        os.environ["NIGHTSHIFT_LEVEL5_ALLOWANCE"] = "0"
        ce = td.calculate_costs({"total_level_5_finish_sqft": 1000},
                                building_type="commercial")
        li = self._l5(ce)
        self.assertNotIn("ALLOWANCE", li["item"])
        self.assertGreater(li["total"], 0)

    def test_allowance_label_same_price(self):
        os.environ["NIGHTSHIFT_LEVEL5_ALLOWANCE"] = "0"
        base = self._l5(td.calculate_costs(
            {"total_level_5_finish_sqft": 1000}, building_type="commercial"))
        os.environ["NIGHTSHIFT_LEVEL5_ALLOWANCE"] = "1"
        marked = self._l5(td.calculate_costs(
            {"total_level_5_finish_sqft": 1000}, building_type="commercial"))
        self.assertIn("ALLOWANCE", marked["item"])
        self.assertIn("strike", marked["item"])
        self.assertEqual(marked["total"], base["total"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
