"""Tests for residual June-10 review fixes: 3e, 4c, 2c."""
import sys

import Takeoff_DIRECT as T
from config import HARD_NUMBERS_ONLY

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


print("3e — free-text unit-multiplier note-parse gated under HARD_NUMBERS_ONLY")
# explicit schema field is a hard number -> always honored
check("explicit unit_multiplier field honored", T._extract_multiplier_from_notes({"unit_multiplier": 5}) == 5)
# free-text note must NOT multiply under the policy (default True)
m = T._extract_multiplier_from_notes({"notes": "28 identical units total"})
if HARD_NUMBERS_ONLY:
    check("'28 units total' note -> 1 under HARD_NUMBERS_ONLY", m == 1, f"got {m}")
else:
    check("policy off -> note parses (env-dependent)", m in (1, 28))
check("no signal -> 1", T._extract_multiplier_from_notes({}) == 1)

print("4c — sheet number regex + normalizer handle revision suffixes")
def sheet(s):
    m = T._SHEET_NUMBER_RE.search(s)
    return (m.group(1).upper(), m.group(2)) if m else None
check("A-101A matches -> base (A,101)", sheet("A-101A") == ("A", "101"), str(sheet("A-101A")))
check("A2.01a matches -> (A,2.01) not truncated", sheet("A2.01a") == ("A", "2.01"), str(sheet("A2.01a")))
check("A-101 still (A,101)", sheet("A-101") == ("A", "101"))
check("normalize A-101A == A-101", T._normalize_sheet_token("A-101A") == T._normalize_sheet_token("A-101") == "A101",
      T._normalize_sheet_token("A-101A"))
check("normalize A2.01a == A201", T._normalize_sheet_token("A2.01a") == "A201", T._normalize_sheet_token("A2.01a"))
check("non-revision letters preserved (PT)", T._normalize_sheet_token("PT") == "PT")

print("2c — canonical sheet token unifies A-102 / A1.02")
check("A-102 == A1.02 normalized", T._normalize_sheet_token("A-102") == T._normalize_sheet_token("A1.02") == "A102")

print("1d — paint-plan sheets rescued by the Division-9 override")
title = "PT-101 PAINT PLAN".lower()
check("paint-plan title matches a rescue keyword", any(kw in title for kw in T._DIVISION_9_KEYWORDS))
check("'paint plan' in keyword list", "paint plan" in T._DIVISION_9_KEYWORDS)
# a real plumbing sheet must NOT match any rescue keyword
check("plumbing sheet not falsely rescued",
      not any(kw in "p-101 plumbing plan / waste & vent riser diagram"
              for kw in T._DIVISION_9_KEYWORDS))

print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED: ' + ', '.join(_fails)} ===")
sys.exit(1 if _fails else 0)
