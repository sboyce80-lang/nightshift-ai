#!/usr/bin/env python3
"""S1 (2026-08-21): template-vs-instance dedup tests — Homewood shape."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _tpl_room(name, ut, mult, wall=350, ceil=120):
    return {"room_id": name, "room_name": name, "unit_type": ut,
            "unit_multiplier": mult, "in_scope": True,
            "dimensions": {"wall_area_sqft": wall,
                           "ceiling_area_sqft": ceil},
            "elements": {"doors_full_paint": 2, "wallcovering_sqft": 200}}


def _an(rooms, total_units=109, agg=None):
    return {"project_info": {"total_units": total_units},
            "floors": [{"floor_name": "all", "rooms": rooms}],
            "aggregated_totals": agg or {}, "notes": []}


def _homewood():
    rooms = [
        _tpl_room("King 1BR Suite - Living/Sleeping", "King One Bedroom Suite", 109),
        _tpl_room("King 1BR Suite - Bathroom", "King One Bedroom Suite", 109),
    ]
    # 6 drawn instances (multiplier 1) of the same type + 1 common room
    for n in (201, 202, 203):
        rooms.append(_tpl_room(f"King 1BR Suite {n} - Living/Sleeping",
                               "", 1))
        rooms.append(_tpl_room(f"Accessible King 1BR Suite {n} - Bathroom",
                               "", 1))
    rooms.append(_tpl_room("Corridor (North Wing)", "common_area", 1))
    agg = {"total_paintable_wall_sqft": 350 * 2 * 109 + 350 * 7,
           "total_wallcovering_sqft": 200 * 2 * 109 + 200 * 7,
           "total_doors_full_paint": 2 * 2 * 109 + 2 * 7}
    return _an(rooms, agg=agg)


class TestTemplateInstanceDedup(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_TEMPLATE_INSTANCE_DEDUP"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_TEMPLATE_INSTANCE_DEDUP", None)

    def test_homewood_shape_drops_instances(self):
        a = _homewood()
        out = td._dedup_template_instances(a)
        rec = out["_template_instance_dedup"]
        self.assertEqual(rec["dropped_rooms"], 6)  # corridor untouched
        rooms = out["floors"][0]["rooms"]
        # templates stay, numbered instances excluded, corridor stays
        self.assertTrue(all(r["in_scope"] for r in rooms[:2]))
        self.assertTrue(all(r["in_scope"] is False for r in rooms[2:8]))
        self.assertTrue(rooms[8]["in_scope"])
        # aggregates reduced by the 6 dropped rooms' contributions
        agg = out["aggregated_totals"]
        self.assertEqual(agg["total_paintable_wall_sqft"],
                         350 * 2 * 109 + 350 * 1)
        self.assertEqual(agg["total_wallcovering_sqft"],
                         200 * 2 * 109 + 200 * 1)
        self.assertEqual(agg["total_doors_full_paint"], 2 * 2 * 109 + 2)
        self.assertTrue(out.get("manual_review_required"))

    def test_prefers_instances_when_template_overcounts(self):
        # template says x40, but the building has 12 units and 36 instance
        # rooms (~12 units) are drawn -> instances win, template dropped
        rooms = [_tpl_room("Studio - Living", "Studio Unit", 40)]
        for n in range(36):
            rooms.append(_tpl_room(f"Studio Unit {200 + n} - Living", "", 1))
        a = _an(rooms, total_units=12)
        out = td._dedup_template_instances(a)
        self.assertFalse(out["floors"][0]["rooms"][0]["in_scope"])
        self.assertTrue(all(r["in_scope"]
                            for r in out["floors"][0]["rooms"][1:]))

    def test_no_instances_noop(self):
        a = _an([_tpl_room("King 1BR Suite - Living", "King One Bedroom Suite",
                           109),
                 _tpl_room("Corridor", "common_area", 1)])
        out = td._dedup_template_instances(a)
        self.assertEqual(out["_template_instance_dedup"]["dropped_rooms"], 0)
        self.assertFalse(out.get("manual_review_required"))

    def test_schedule_authoritative_doors_untouched(self):
        a = _homewood()
        a["_schedule_authoritative_counts"] = {"total_doors_full_paint": 197}
        before = a["aggregated_totals"]["total_doors_full_paint"]
        out = td._dedup_template_instances(a)
        self.assertEqual(out["aggregated_totals"]["total_doors_full_paint"],
                         before)

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_TEMPLATE_INSTANCE_DEDUP"] = "0"
        a = _homewood()
        out = td._dedup_template_instances(a)
        self.assertNotIn("_template_instance_dedup", out)

    def test_idempotent(self):
        a = _homewood()
        once = td._dedup_template_instances(a)
        walls = once["aggregated_totals"]["total_paintable_wall_sqft"]
        twice = td._dedup_template_instances(once)
        self.assertEqual(twice["aggregated_totals"]
                         ["total_paintable_wall_sqft"], walls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
