"""Regression tests for the end-of-run failed-sheet retry
(NIGHTSHIFT_FAILED_SHEET_RETRY).

Per-sheet extraction marks a page failed when _extract_single_sheet returns
None; before this feature those pages simply shipped as coverage holes (Otto
Cadillac 2026-07-21: pages 28-29 — the door/finish-schedule area sheets —
failed once each, so the estimate went out with no door counts or wall
finishes and the coverage gate forced manual review). The retry re-attempts
each failed page after the rest of the run completes, since the in-call
_call_sheet_api ladder was already exhausted on the first attempt.

These lock the deterministic primitives:
  _failed_sheet_retry_enabled  — flag, default OFF.
  _failed_sheet_retry_rounds   — env-tunable rounds, clamped 1-3.
  _retry_failed_sheets         — the scheduler: round accounting, early
    exit, exception isolation, recovered/still-failed split.
Offline, no API.
"""
import os
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# 1) Flag default OFF.
os.environ.pop("NIGHTSHIFT_FAILED_SHEET_RETRY", None)
check(T._failed_sheet_retry_enabled() is False, "retry flag should default off")
os.environ["NIGHTSHIFT_FAILED_SHEET_RETRY"] = "1"
check(T._failed_sheet_retry_enabled() is True, "retry flag '1' should enable")
os.environ.pop("NIGHTSHIFT_FAILED_SHEET_RETRY", None)

# 2) Rounds: default 2, clamped to 1-3, garbage falls back to 2.
os.environ.pop("NIGHTSHIFT_FAILED_SHEET_RETRY_N", None)
check(T._failed_sheet_retry_rounds() == 2, "rounds should default to 2")
for raw, want in (("0", 1), ("1", 1), ("3", 3), ("99", 3), ("-5", 1),
                  ("garbage", 2)):
    os.environ["NIGHTSHIFT_FAILED_SHEET_RETRY_N"] = raw
    got = T._failed_sheet_retry_rounds()
    check(got == want, f"rounds({raw!r}) = {got}, want {want}")
os.environ.pop("NIGHTSHIFT_FAILED_SHEET_RETRY_N", None)

# 3) Scheduler: everything recovers in round 1; no second-round calls.
calls = []
rec, still = T._retry_failed_sheets([27, 28], lambda pg: calls.append(pg) or True, 3)
check(rec == [27, 28] and still == [], f"round-1 recovery wrong: {rec}/{still}")
check(calls == [27, 28], f"recovered pages re-attempted: {calls}")

# 4) Scheduler: fails round 1, recovers round 2 (transient brownout shape).
attempts = {}
def _second_try(pg):
    attempts[pg] = attempts.get(pg, 0) + 1
    return attempts[pg] >= 2
rec, still = T._retry_failed_sheets([27, 28], _second_try, 2)
check(rec == [27, 28] and still == [], f"round-2 recovery wrong: {rec}/{still}")
check(attempts == {27: 2, 28: 2}, f"attempt counts wrong: {attempts}")

# 5) Scheduler: persistent failure with rounds=1 stays failed.
rec, still = T._retry_failed_sheets([27, 28], lambda pg: False, 1)
check(rec == [] and still == [27, 28], f"persistent failure wrong: {rec}/{still}")

# 6) Scheduler: an attempt that RAISES counts as failed for that page but
#    does not abort the pass — the other page still recovers.
def _boom_on_27(pg):
    if pg == 27:
        raise RuntimeError("render blew up")
    return True
rec, still = T._retry_failed_sheets([27, 28], _boom_on_27, 1)
check(rec == [28] and still == [27], f"exception isolation wrong: {rec}/{still}")

# 7) Scheduler: mixed outcome across rounds — 27 recovers round 1, 28 only
#    on round 3, but rounds=2 leaves it failed.
attempts = {}
def _slowpoke(pg):
    attempts[pg] = attempts.get(pg, 0) + 1
    return pg == 27 or attempts[pg] >= 3
rec, still = T._retry_failed_sheets([27, 28], _slowpoke, 2)
check(rec == [27] and still == [28], f"mixed outcome wrong: {rec}/{still}")
check(attempts.get(28) == 2, f"28 should get exactly 2 attempts: {attempts}")

# 8) Scheduler: empty failed list -> no calls, empty result; rounds<1 coerced.
calls = []
rec, still = T._retry_failed_sheets([], lambda pg: calls.append(pg) or True, 2)
check(rec == [] and still == [] and calls == [], "empty input should no-op")
rec, still = T._retry_failed_sheets([5], lambda pg: True, 0)
check(rec == [5], "rounds=0 should still run one pass")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
