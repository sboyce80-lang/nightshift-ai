#!/usr/bin/env python3
"""F1 (2026-08-20 JW batch): parse-robustness regression tests.

Failure shapes reproduced from the batch logs:
  - Harlem: '```json\\n{"room_finish_schedule": [], ...' failed at 1,968 chars
  - Harlem/Hudson/Caris: 68-95k-char responses opening with narration
    ("I'll systematically analyze...") failed all repair stages
  - ULUM: short (6-10k) responses unparseable
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NIGHTSHIFT_TEST_MODE", "1")

import Takeoff_DIRECT as td

PAYLOAD = {"project_info": {"total_rooms_found": 2},
           "floors": [{"floor_name": "Ground",
                       "rooms": [{"room_id": "101"}, {"room_id": "102"}]}]}
PJ = json.dumps(PAYLOAD)


class TestMultiObjectScan(unittest.TestCase):
    def test_narration_prefix_and_suffix(self):
        text = ("I'll systematically analyze sheet 18-A201, which contains "
                "both a Ground Floor Plan and Basement.\n\n" + PJ +
                "\n\nThis completes my analysis of the sheet.")
        self.assertEqual(td._parse_json_response(text), PAYLOAD)

    def test_narration_with_incidental_braces_before_payload(self):
        text = ('First I note the legend shows {"example": 1} as a format.\n'
                "Now the extraction:\n" + PJ)
        self.assertEqual(td._parse_json_response(text), PAYLOAD)

    def test_two_objects_prefers_signal_keys(self):
        decoy = json.dumps({"summary": "x" * 500})
        text = decoy + "\nand the real payload:\n" + PJ
        self.assertEqual(td._parse_json_response(text), PAYLOAD)

    def test_multiple_fenced_blocks(self):
        text = ("Here is the schedule:\n```json\n"
                + json.dumps({"room_finish_schedule": []}) +
                "\n```\nAnd the rooms:\n```json\n" + PJ + "\n```\n")
        out = td._parse_json_response(text)
        # first-fence-wins is acceptable; must be one of the two signal
        # payloads, never a mangled merge
        self.assertIn(out, [{"room_finish_schedule": []}, PAYLOAD])

    def test_trailing_junk_after_balanced_object(self):
        text = PJ + "\n\nNote: dimensions verified against the plan."
        self.assertEqual(td._parse_json_response(text), PAYLOAD)


class TestTruncationRepair(unittest.TestCase):
    def test_unterminated_fence_truncated_payload(self):
        # Harlem shape: fenced JSON, response cut before the closing fence
        full = json.dumps({"room_finish_schedule": [{"room": "101"}],
                           "structural_finish_scope": [],
                           "building": "Farm Hub"})
        text = "```json\n" + full[:len(full) - 25]
        out = td._parse_json_response(text)
        self.assertIsInstance(out, dict)
        self.assertIn("room_finish_schedule", out)

    def test_truncated_after_narration_with_earlier_braces(self):
        # the walker used to anchor at the narration's first "{"
        big = json.dumps({"floors": [{"floor_name": "L1",
                                      "rooms": [{"room_id": str(i)}
                                                for i in range(50)]}]})
        text = ('Reading the legend {"scale": "1/8in"} first.\n' +
                big[:len(big) - 40])
        out = td._parse_json_response(text)
        self.assertIsInstance(out, dict)
        self.assertIn("floors", out)
        self.assertGreater(len(out["floors"][0]["rooms"]), 30)

    def test_hopeless_input_returns_none(self):
        self.assertIsNone(td._parse_json_response("no json here at all"))
        self.assertIsNone(td._parse_json_response(""))


class TestBalancedObjectScanner(unittest.TestCase):
    def test_bounded_on_pathological_input(self):
        # 95k chars of stray braces must not hang or return garbage
        text = ("{ " * 40000)
        self.assertEqual(td._iter_balanced_objects(text), [])

    def test_braces_inside_strings_ignored(self):
        obj = {"notes": "wall {height} varies", "floors": []}
        text = "prefix " + json.dumps(obj) + " suffix"
        spans = td._iter_balanced_objects(text)
        self.assertEqual(json.loads(spans[0]), obj)


class TestStructuredOutputLadder(unittest.TestCase):
    def setUp(self):
        td._STRUCTURED_OUTPUTS_MODE = "full"
        td._STRUCTURED_OUTPUTS_BROKEN = False

    def tearDown(self):
        td._STRUCTURED_OUTPUTS_MODE = "full"
        td._STRUCTURED_OUTPUTS_BROKEN = False

    def _grammar_400(self):
        import httpx
        resp = httpx.Response(
            400, request=httpx.Request("POST", "https://api.anthropic.com"))
        return td.anthropic.BadRequestError(
            message=("Error code: 400 - {'type': 'error', 'error': "
                     "{'message': 'The compiled grammar is too large'}}"),
            response=resp, body=None)

    def test_full_steps_to_slim_then_off(self):
        exc = self._grammar_400()
        self.assertTrue(td._maybe_disable_structured_outputs(exc))
        self.assertEqual(td._STRUCTURED_OUTPUTS_MODE, "slim")
        kw = td._extraction_output_kwargs()
        self.assertIs(kw["output_config"]["format"]["schema"],
                      td._EXTRACTION_OUTPUT_SCHEMA_SLIM)
        self.assertTrue(td._maybe_disable_structured_outputs(exc))
        self.assertEqual(td._STRUCTURED_OUTPUTS_MODE, "off")
        self.assertEqual(td._extraction_output_kwargs(), {})
        # third call: already off — no further stepping
        self.assertFalse(td._maybe_disable_structured_outputs(exc))

    def test_unrelated_400_does_not_step(self):
        import httpx
        resp = httpx.Response(
            400, request=httpx.Request("POST", "https://api.anthropic.com"))
        exc = td.anthropic.BadRequestError(
            message="Error code: 400 - image exceeds maximum size",
            response=resp, body=None)
        self.assertFalse(td._maybe_disable_structured_outputs(exc))
        self.assertEqual(td._STRUCTURED_OUTPUTS_MODE, "full")

    def test_slim_schema_room_shape_matches_full(self):
        full_rooms = (td._EXTRACTION_OUTPUT_SCHEMA["properties"]["floors"]
                      ["items"]["properties"]["rooms"])
        slim_rooms = (td._EXTRACTION_OUTPUT_SCHEMA_SLIM["properties"]
                      ["floors"]["items"]["properties"]["rooms"])
        self.assertEqual(full_rooms, slim_rooms)


if __name__ == "__main__":
    unittest.main(verbosity=2)
