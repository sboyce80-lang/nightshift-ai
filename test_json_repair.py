#!/usr/bin/env python3
"""Offline tests for the JSON repair parser + truncation helpers.

Pins the Phase 1(a) behavior: model responses that are prose-wrapped,
fenced, trailing-comma'd, or cut off at max_tokens must still parse —
the bare regex+json.loads pattern silently dropped entire batches/chunks
on any imperfection (observed live: a 9-tile enhanced batch returning
"could not parse JSON" -> 0 rooms on the 2026-06-12 validation run).

Run: python3 test_json_repair.py
"""
import importlib.util as iu
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

P = T._parse_json_response
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    # Clean / wrapped / fenced / trailing-comma
    check("clean object", P('{"a": 1}') == {"a": 1})
    check("prose-wrapped", P('Result:\n{"a": [1,2]}\nDone.') == {"a": [1, 2]})
    check("fenced block", P('```json\n{"a": 1}\n```') == {"a": 1})
    check("trailing commas", P('{"a": [1,2,],}') == {"a": [1, 2]})
    check("escaped quotes", (P('{"note": "say \\"hi\\" ok", "a": 1}') or {}).get("a") == 1)
    check("non-dict top level rejected", P('[1, 2, 3]') is None)
    check("hopeless input", P("no json here at all") is None)

    # Truncation: cut mid-number
    t1 = ('{"floors": [{"floor_name": "L1", "rooms": ['
          '{"room_id": "A", "wall": 100}, {"room_id": "B", "wall": 2')
    r1 = P(t1, "t1")
    check("truncated mid-number keeps complete rooms",
          bool(r1) and r1["floors"][0]["rooms"][0]["room_id"] == "A", repr(r1))

    # Truncation: cut mid-string (dangling key must be discarded)
    t2 = ('{"floors": [{"floor_name": "L1", "rooms": ['
          '{"room_id": "A"}, {"room_id": "Off')
    r2 = P(t2, "t2")
    check("truncated mid-string discards dangling fragment",
          bool(r2) and r2["floors"][0]["rooms"] == [{"room_id": "A"}], repr(r2))

    # Realistic scale: 50 rooms, cut at 60% — most complete rooms recovered
    rooms = ",".join('{"room_id": "R%d", "dims": {"wall": %d}}' % (i, i * 10)
                     for i in range(50))
    full = '{"floors": [{"floor_name": "L1", "rooms": [' + rooms + "]}]}"
    cut = full[: int(len(full) * 0.6)]
    r3 = P(cut, "t3")
    n = len(r3["floors"][0]["rooms"]) if r3 else 0
    check("60%-cut 50-room response recovers ~60% of rooms",
          25 <= n <= 31, f"recovered={n}")

    # TruncatedResponseError carries partial text for salvage
    err = T.TruncatedResponseError("cut", partial_text=t1)
    check("TruncatedResponseError.partial_text carried",
          err.partial_text == t1)

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
