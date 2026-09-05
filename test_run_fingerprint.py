#!/usr/bin/env python3
"""Run fingerprint: which code, under which flags, produced a result.

Locks in the three traceability guarantees added 2026-09-04:
  1. _nightshift_flag_vector() captures every NIGHTSHIFT_* env var except
     runtime-position state (draw tag/active, progress file), sorted, read
     at call time so a per-job resolver posture is captured as applied.
  2. _flag_fingerprint() is stable for the same posture and changes when
     any flag's value changes — including 0 vs absent, which are different
     postures (CEILING_ASSUME_PAINTED taught us that).
  3. _sheet_checkpoint_key() embeds the fingerprint: a checkpoint written
     under one posture must never replay under another (the R4 stale-merge
     replay class), while retries within one posture still resume.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The module reads flags at call time; make sure our probe flags are clean.
for k in list(os.environ):
    if k.startswith("NIGHTSHIFT_TESTPROBE_"):
        del os.environ[k]

import Takeoff_DIRECT as TD  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


print("run fingerprint checks")

# --- 1. vector contents -----------------------------------------------------
os.environ["NIGHTSHIFT_TESTPROBE_A"] = "1"
os.environ["NIGHTSHIFT_JOB_DRAW_TAG"] = "draw2"
os.environ["NIGHTSHIFT_PROGRESS_FILE"] = "/tmp/p.json"
vec = TD._nightshift_flag_vector()
check("NIGHTSHIFT_TESTPROBE_A" in vec,
      "vector captures a set NIGHTSHIFT flag: missing")
check("NIGHTSHIFT_JOB_DRAW_TAG" not in vec,
      "vector excludes the draw tag (runtime position, not posture)")
check("NIGHTSHIFT_PROGRESS_FILE" not in vec,
      "vector excludes the progress file path")
check(list(vec) == sorted(vec),
      "vector is sorted for stable hashing")

# --- 2. fingerprint semantics ----------------------------------------------
fp1 = TD._flag_fingerprint()
fp1_again = TD._flag_fingerprint()
check(fp1 == fp1_again, "fingerprint is stable for an unchanged posture")

os.environ["NIGHTSHIFT_TESTPROBE_A"] = "0"
fp_zero = TD._flag_fingerprint()
check(fp_zero != fp1,
      "fingerprint changes when a flag's value changes (1 -> 0)")

del os.environ["NIGHTSHIFT_TESTPROBE_A"]
fp_absent = TD._flag_fingerprint()
check(fp_absent != fp_zero,
      "explicit 0 and absent are different postures")

os.environ["NIGHTSHIFT_JOB_DRAW_TAG"] = "draw3"
check(TD._flag_fingerprint() == fp_absent,
      "changing the draw tag does not move the fingerprint")

# --- 3. checkpoint key embeds the posture ----------------------------------
os.environ.pop("NIGHTSHIFT_JOB_DRAW_TAG", None)
key_base = TD._sheet_checkpoint_key("PROMPT", "CTX", True)
check(key_base == TD._sheet_checkpoint_key("PROMPT", "CTX", True),
      "checkpoint key is stable within one posture (retries resume)")

os.environ["NIGHTSHIFT_TESTPROBE_A"] = "1"
key_flipped = TD._sheet_checkpoint_key("PROMPT", "CTX", True)
check(key_flipped != key_base,
      "checkpoint key changes when the flag posture changes")
del os.environ["NIGHTSHIFT_TESTPROBE_A"]
check(TD._sheet_checkpoint_key("PROMPT", "CTX", True) == key_base,
      "checkpoint key returns when the posture returns (same-posture replay)")

# --- 4. git sha is best-effort, never a guess ------------------------------
sha = TD._git_sha()
check(isinstance(sha, str), "git sha is a string")
check(sha == "" or len(sha) == 12, "git sha is empty or a 12-char prefix")

prev = os.environ.get("RENDER_GIT_COMMIT")
os.environ["RENDER_GIT_COMMIT"] = "abcdef0123456789abcd"
check(TD._git_sha() == "abcdef012345",
      "RENDER_GIT_COMMIT wins when present (worker has no .git)")
if prev is None:
    del os.environ["RENDER_GIT_COMMIT"]
else:
    os.environ["RENDER_GIT_COMMIT"] = prev

print()
if fails:
    print(f"❌ {len(fails)} run fingerprint check(s) failed")
    sys.exit(1)
print("✅ all run fingerprint checks passed")
