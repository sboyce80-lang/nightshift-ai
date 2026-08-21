#!/usr/bin/env python3
"""R2 (2026-08-21): per-sheet consensus merge tests."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _read(rooms, floor="1st"):
    return {"project_info": {"total_rooms_found": len(rooms)},
            "floors": [{"floor_name": floor, "rooms": rooms}]}


def _room(rid, doors=0, wall=0):
    return {"room_id": rid, "room_name": rid, "in_scope": True,
            "dimensions": {"wall_area_sqft": wall},
            "elements": {"doors_full_paint": doors}}


class TestSheetConsensusMerge(unittest.TestCase):
    def test_max_recovers_missed_fields(self):
        a = _read([_room("101", doors=2, wall=0)])
        b = _read([_room("101", doors=0, wall=400)])
        m = td._merge_sheet_consensus_reads([a, b])
        r = m["floors"][0]["rooms"][0]
        self.assertEqual(r["elements"]["doors_full_paint"], 2)
        self.assertEqual(r["dimensions"]["wall_area_sqft"], 400)

    def test_union_adds_room_seen_once(self):
        a = _read([_room("101", doors=1)])
        b = _read([_room("101", doors=1), _room("102", doors=3)])
        m = td._merge_sheet_consensus_reads([a, b])
        ids = [r["room_id"] for r in m["floors"][0]["rooms"]]
        self.assertEqual(sorted(ids), ["101", "102"])

    def test_caris_shape_door_stability(self):
        # read1 sees 34+34 doors, read2 collapses to 2 — consensus keeps 68
        a = _read([_room("D1", doors=34), _room("D2", doors=34)])
        b = _read([_room("D1", doors=2)])
        m = td._merge_sheet_consensus_reads([a, b])
        total = sum(r["elements"]["doors_full_paint"]
                    for r in m["floors"][0]["rooms"])
        self.assertEqual(total, 68)

    def test_project_info_max(self):
        a = _read([_room("101")])
        b = _read([_room("101")])
        b["project_info"]["total_units"] = 109
        a["project_info"]["total_units"] = 34
        m = td._merge_sheet_consensus_reads([a, b])
        self.assertEqual(m["project_info"]["total_units"], 109)


if __name__ == "__main__":
    unittest.main(verbosity=2)
