#!/usr/bin/env python3
"""F5 (2026-08-20 JW batch): floor-finish → pricing wiring tests.

ULUM shape: extraction note "EP-1 (Epoxy) confirmed in Kitchen [1-09] and
Kitchen Storage [1-10]" recorded, both rooms priced $0 floors."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


def _mk(notes=None, rooms=None):
    return {
        "notes": notes or [],
        "floors": [{"floor_name": "Ground", "rooms": rooms or []}],
        "aggregated_totals": {"total_concrete_floor_sqft": 0},
    }


def _room(rid, name, area, note="", conc=0, in_scope=True):
    return {"room_id": rid, "room_name": name, "in_scope": in_scope,
            "notes": note,
            "dimensions": {"floor_area_sqft": area},
            "elements": {"concrete_floor_sqft": conc},
            "materials": {}}


class TestFloorFinishReconcile(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_FLOOR_FINISH_RECONCILE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_FLOOR_FINISH_RECONCILE", None)

    def test_flag_off_is_noop(self):
        os.environ["NIGHTSHIFT_FLOOR_FINISH_RECONCILE"] = "0"
        a = _mk(notes=["EP-1 (Epoxy) confirmed in Kitchen [1-09]"],
                rooms=[_room("1-09", "Kitchen", 712.5)])
        before = copy.deepcopy(a)
        self.assertEqual(td._reconcile_floor_finishes(a), before)

    def test_ulum_sheet_note_wires_named_rooms(self):
        a = _mk(notes=["FLOOR FINISHES NOTED: EP-1 (Epoxy) confirmed in "
                       "Kitchen [1-09] and Kitchen Storage [1-10]; PC-1 "
                       "(Polished Concrete) confirmed in Bar [1-02]"],
                rooms=[_room("1-09", "Kitchen", 712.5),
                       _room("1-10", "Kitchen Storage", 264.0),
                       _room("1-02", "Bar", 500.0)])
        out = td._reconcile_floor_finishes(a)
        rooms = out["floors"][0]["rooms"]
        self.assertEqual(rooms[0]["elements"]["concrete_floor_sqft"], 712.5)
        self.assertEqual(rooms[1]["elements"]["concrete_floor_sqft"], 264.0)
        # polished stays excluded, but the RFI note must exist
        self.assertEqual(rooms[2]["elements"]["concrete_floor_sqft"], 0)
        self.assertEqual(out["aggregated_totals"]["total_concrete_floor_sqft"],
                         976.5)
        joined = " ".join(out["notes"])
        self.assertIn("Floor-Finish Reconcile", joined)
        self.assertIn("Polished concrete", joined)

    def test_room_level_evidence(self):
        a = _mk(rooms=[_room("101", "Mech", 300,
                             note="Floor: SC-1 sealed concrete per schedule")])
        out = td._reconcile_floor_finishes(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 300)

    def test_no_evidence_no_change(self):
        a = _mk(notes=["General note about ceilings"],
                rooms=[_room("101", "Office", 200)])
        out = td._reconcile_floor_finishes(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 0)
        self.assertFalse([n for n in out["notes"]
                          if "Floor-Finish" in str(n)])

    def test_never_overwrites_existing_quantity(self):
        a = _mk(notes=["EP-1 confirmed in Kitchen [1-09]"],
                rooms=[_room("1-09", "Kitchen", 712.5, conc=650)])
        out = td._reconcile_floor_finishes(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 650)

    def test_out_of_scope_room_untouched(self):
        a = _mk(notes=["EP-1 confirmed in Kitchen [1-09]"],
                rooms=[_room("1-09", "Kitchen", 712.5, in_scope=False)])
        out = td._reconcile_floor_finishes(a)
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 0)

    def test_idempotent(self):
        a = _mk(notes=["EP-1 confirmed in Kitchen [1-09]"],
                rooms=[_room("1-09", "Kitchen", 712.5)])
        once = td._reconcile_floor_finishes(a)
        note_count = len(once["notes"])
        twice = td._reconcile_floor_finishes(copy.deepcopy(once))
        self.assertEqual(len(twice["notes"]), note_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
