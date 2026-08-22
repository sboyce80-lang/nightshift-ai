#!/usr/bin/env python3
"""S3 (2026-08-21): geometric room completion — Harlem starved-ceilings."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _room(name, floor_area=0, ceil_painted=True, ceil="GYP", in_scope=True):
    return {"room_id": name, "room_name": name, "in_scope": in_scope,
            "notes": "",
            "materials": {"ceiling_painted": ceil_painted, "ceiling": ceil},
            "dimensions": {"floor_area_sqft": floor_area,
                           "ceiling_area_sqft": 0},
            "elements": {}}


def _an(rooms, shadow_rooms, swings=0, doors_priced=0):
    return {
        "floors": [{"floor_name": "G", "rooms": rooms}],
        "aggregated_totals": {"total_paintable_ceiling_sqft": 0,
                              "total_doors_full_paint": doors_priced},
        "notes": [],
        "_room_geometry_shadow": {"engine": "room-geometry-shadow-v1",
                                  "pages": [{
            "pdf": "plans_clean.pdf", "page": 1,
            "rooms": shadow_rooms,
            "doors": {"swing_count": swings}}]},
    }


class TestGeometricRoomCompletion(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_GEOMETRIC_ROOM_COMPLETION"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_GEOMETRIC_ROOM_COMPLETION", None)

    def test_starved_room_completed_from_polygon(self):
        a = _an([_room("Wash Pack"), _room("Open Office")],
                {"Wash Pack": {"area_sqft": 1200, "status": "ok"},
                 "Open Office": {"area_sqft": 800, "status": "ok"}})
        out = td._apply_geometric_room_completion(a)
        r = out["floors"][0]["rooms"][0]
        self.assertEqual(r["dimensions"]["floor_area_sqft"], 1200)
        self.assertEqual(r["dimensions"]["ceiling_area_sqft"], 1200)
        self.assertEqual(out["aggregated_totals"]
                         ["total_paintable_ceiling_sqft"], 2000)
        self.assertEqual(out["_geometric_room_completion"]
                         ["completed_rooms"], 2)

    def test_act_ceiling_gets_floor_but_no_ceiling(self):
        a = _an([_room("Vehicle Shop", ceil_painted=False, ceil="ACT")],
                {"Vehicle Shop": {"area_sqft": 900, "status": "ok"}})
        out = td._apply_geometric_room_completion(a)
        r = out["floors"][0]["rooms"][0]
        self.assertEqual(r["dimensions"]["floor_area_sqft"], 900)
        self.assertEqual(r["dimensions"]["ceiling_area_sqft"], 0)
        self.assertEqual(out["aggregated_totals"]
                         ["total_paintable_ceiling_sqft"], 0)

    def test_non_starved_room_untouched(self):
        a = _an([_room("Office", floor_area=500)],
                {"Office": {"area_sqft": 999, "status": "ok"}})
        out = td._apply_geometric_room_completion(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["dimensions"]
                         ["floor_area_sqft"], 500)

    def test_unmeasured_status_skipped(self):
        a = _an([_room("Storage")],
                {"Storage": {"area_sqft": 300, "status": "unresolved"}})
        out = td._apply_geometric_room_completion(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["dimensions"]
                         ["floor_area_sqft"], 0)

    def test_door_swing_rfi_when_starved(self):
        a = _an([_room("R", floor_area=100)], {}, swings=29, doors_priced=10)
        out = td._apply_geometric_room_completion(a)
        # no shadow rooms -> noop for completion, but record exists? pages
        # without 'rooms' filter out entirely -> noop
        self.assertIn("_geometric_room_completion", out)

    def test_door_swing_rfi_fires_with_rooms(self):
        a = _an([_room("R")],
                {"R": {"area_sqft": 100, "status": "ok"}},
                swings=29, doors_priced=10)
        out = td._apply_geometric_room_completion(a)
        self.assertTrue(out.get("manual_review_required"))
        self.assertTrue(any("door swings" in str(r.get("question", ""))
                            for r in (out.get("_pre_pricing_rfis") or [])))

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_GEOMETRIC_ROOM_COMPLETION"] = "0"
        a = _an([_room("R")], {"R": {"area_sqft": 100, "status": "ok"}})
        out = td._apply_geometric_room_completion(a)
        self.assertNotIn("_geometric_room_completion", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
