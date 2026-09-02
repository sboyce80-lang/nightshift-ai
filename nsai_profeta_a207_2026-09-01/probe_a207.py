#!/usr/bin/env python3
"""Targeted A/B of the room-finish read on 168 Holley St.

Does NOT run the full takeoff — it exercises exactly the link the offline
tests could not: whether the model actually reads A-207's per-room F/B/W tag
blocks with the finish-PLAN prompt, and what page each mode selects.

  OFF -> pre-fix: selects p16 (A-301 EXTERIOR ELEVATIONS, a sheet that
         failed extraction in the 2026-09-01 production run)
  ON  -> fixed:   selects p14 (A-207 FINISH PLANS & SPECIFICATIONS)
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PDF = os.path.join(HERE, "plans_clean.pdf")
mode = sys.argv[1] if len(sys.argv) > 1 else "1"
os.environ["NIGHTSHIFT_FINISH_PLAN_DISCOVERY"] = mode

import anthropic
from config import CLAUDE_API_KEY
import Takeoff_DIRECT as T

pages = T._find_finish_schedule_pages(PDF)
print(f"[flag={mode}] detect={T._detect_finish_schedule(PDF)} "
      f"pages(1-based)={[p + 1 for p in pages]}", flush=True)

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
t0 = time.time()
res = T._extract_room_finish_schedule(client, PDF, page_indices=pages)
rows = (res or {}).get("room_finish_schedule") or []
print(f"[flag={mode}] rows={len(rows)}  elapsed={time.time() - t0:.0f}s", flush=True)

out = os.path.join(HERE, f"rfs_flag{mode}.json")
json.dump(res, open(out, "w"), indent=1)
for r in rows[:14]:
    print("   {:22} wall={:22} base={:20} ceil={}".format(
        str(r.get("room_name"))[:22], str(r.get("wall_finish"))[:22],
        str(r.get("base_finish"))[:20], str(r.get("ceiling_finish"))[:18]))
print(f"wrote {out}")
