#!/usr/bin/env python3
"""V1-V4 (variance-reduction block, 2026-08-22)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _read(rooms):
    return {"project_info": {}, "floors": [{"floor_name": "1",
                                            "rooms": rooms}]}


def _room(rid, doors=0, wall=0):
    return {"room_id": rid, "room_name": rid, "in_scope": True,
            "dimensions": {"wall_area_sqft": wall},
            "elements": {"doors_full_paint": doors}}


class TestMedianConsensusN3(unittest.TestCase):
    def test_outlier_read_loses_the_vote(self):
        # Hudson shape: [34, 34, 2] → median 34; [300, 120, 127] → 127
        a = _read([_room("D1", doors=34, wall=400)])
        b = _read([_room("D1", doors=34, wall=410)])
        c = _read([_room("D1", doors=2, wall=1200)])
        m = td._merge_sheet_consensus_reads([a, b, c])
        r = m["floors"][0]["rooms"][0]
        self.assertEqual(r["elements"]["doors_full_paint"], 34)
        self.assertEqual(r["dimensions"]["wall_area_sqft"], 410)

    def test_absence_votes_zero(self):
        # room in 1 of 3 reads: kept via union, values stand (no median)
        a = _read([_room("D1", doors=5)])
        b = _read([_room("D1", doors=5), _room("Boiler Plant", doors=7)])
        c = _read([_room("D1", doors=5)])
        m = td._merge_sheet_consensus_reads([a, b, c])
        by = {r["room_id"]: r for r in m["floors"][0]["rooms"]}
        self.assertEqual(by["Boiler Plant"]["elements"]["doors_full_paint"],
                         7)
        # room in 2 of 3: absence votes 0 → median of [3,3,0] = 3
        a2 = _read([_room("X", doors=3)])
        b2 = _read([_room("X", doors=3)])
        c2 = _read([])
        m2 = td._merge_sheet_consensus_reads([a2, b2, c2])
        self.assertEqual(m2["floors"][0]["rooms"][0]["elements"]
                         ["doors_full_paint"], 3)

    def test_n2_still_fill_only(self):
        a = _read([_room("D1", doors=0, wall=300)])
        b = _read([_room("D1", doors=4, wall=450)])
        m = td._merge_sheet_consensus_reads([a, b])
        r = m["floors"][0]["rooms"][0]
        self.assertEqual(r["elements"]["doors_full_paint"], 4)  # fill
        self.assertEqual(r["dimensions"]["wall_area_sqft"], 300)  # no raise


class TestPerClassConsensusN(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_PER_SHEET_CONSENSUS", None)
        td._CONSENSUS_JOB_PAGE_COUNT = 0

    def test_dd_set_forces_single_read(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        td._CONSENSUS_JOB_PAGE_COUNT = 49
        self.assertEqual(td._effective_consensus_n(), 1)
        td._CONSENSUS_JOB_PAGE_COUNT = 8
        self.assertEqual(td._effective_consensus_n(), 3)

    def test_checkpoint_key_embeds_consensus(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        td._CONSENSUS_JOB_PAGE_COUNT = 8
        k3 = td._sheet_checkpoint_key("p", "c", True)
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "1"
        k1 = td._sheet_checkpoint_key("p", "c", True)
        self.assertNotEqual(k3, k1)


class TestDoorSwingAuthority(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_GEOMETRIC_ROOM_COMPLETION"] = "1"
        os.environ["NIGHTSHIFT_DOOR_SWING_AUTHORITY"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_GEOMETRIC_ROOM_COMPLETION", None)
        os.environ.pop("NIGHTSHIFT_DOOR_SWING_AUTHORITY", None)

    def _an(self, swings, doors):
        return {"floors": [{"floor_name": "1", "rooms": []}],
                "aggregated_totals": {"total_doors_full_paint": doors},
                "notes": [],
                "_room_geometry_shadow": {"pages": [{
                    "rooms": {"R": {"area_sqft": 1, "status": "measured"}},
                    "doors": {"swing_count": swings}}]}}

    def test_strong_signal_caps(self):
        out = td._apply_geometric_room_completion(self._an(150, 378))
        self.assertAlmostEqual(
            out["aggregated_totals"]["total_doors_full_paint"], 180.0, 1)
        self.assertTrue(any("Door Swing Authority" in str(n)
                            for n in out["notes"]))

    def test_zero_signal_never_caps(self):
        out = td._apply_geometric_room_completion(self._an(0, 378))
        self.assertEqual(out["aggregated_totals"]
                         ["total_doors_full_paint"], 378)

    def test_weak_signal_never_caps(self):
        # 20 swings vs 378 priced: < 30% — untrusted, no cap
        out = td._apply_geometric_room_completion(self._an(20, 378))
        self.assertEqual(out["aggregated_totals"]
                         ["total_doors_full_paint"], 378)

    def test_schedule_authoritative_wins(self):
        a = self._an(150, 378)
        a["_schedule_authoritative_counts"] = {"total_doors_full_paint": 378}
        out = td._apply_geometric_room_completion(a)
        self.assertEqual(out["aggregated_totals"]
                         ["total_doors_full_paint"], 378)


if __name__ == "__main__":
    unittest.main(verbosity=2)
