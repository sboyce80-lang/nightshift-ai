#!/usr/bin/env python3
"""Overnight batch orchestrator: each job runs as a subprocess with a
wall-clock timeout; on timeout/crash it retries once with multi_pass=0.
Checkpoint/resume: jobs with an existing run_meta.json (ok=true) are skipped.
Order: smallest plan set first so quick wins land early.
"""
import os, sys, json, subprocess, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
# (job_key, multi_pass_timeout_s, single_pass_timeout_s)
ORDER = [
    ("harlem_valley",     7200, 5400),
    ("hudson_hotel",      9000, 5400),
    ("caris_hyde_park",   9000, 5400),
    ("under_canvas_ulum", 12600, 7200),
    ("homewood_suites",   12600, 7200),
]

def log(m):
    line = f"[{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)

def meta(job):
    p = os.path.join(HERE, job, "run_meta.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None

def attempt(job, mp, timeout):
    mode = "multi" if mp else "single"
    log(f"START {job} ({mode}-pass, timeout {timeout}s)")
    logf = open(os.path.join(HERE, job, f"run_{mode}.log"), "w")
    try:
        rc = subprocess.run([PY, os.path.join(HERE, "run_one.py"), job,
                             "1" if mp else "0"],
                            stdout=logf, stderr=subprocess.STDOUT,
                            timeout=timeout, cwd=HERE).returncode
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {job} ({mode}-pass) after {timeout}s")
        return "timeout"
    finally:
        logf.close()
    m = meta(job)
    if rc == 0 and m and m.get("ok"):
        log(f"DONE {job}: subtotal=${m.get('subtotal', 0):,.2f} "
            f"manual_review={m.get('manual_review')} in {m.get('elapsed_s')}s")
        return "ok"
    log(f"FAILED {job} ({mode}-pass) rc={rc} "
        f"err={(m or {}).get('error', 'no meta written')}")
    return "failed"

def main():
    t0 = time.time()
    results = {}
    for job, t_multi, t_single in ORDER:
        m = meta(job)
        if m and m.get("ok"):
            log(f"SKIP {job} — already complete (subtotal=${m.get('subtotal',0):,.2f})")
            results[job] = "ok (cached)"
            continue
        status = attempt(job, True, t_multi)
        if status != "ok":
            # stale meta from the failed attempt must not satisfy the checkpoint
            mp_meta = os.path.join(HERE, job, "run_meta.json")
            if os.path.exists(mp_meta):
                os.rename(mp_meta, os.path.join(HERE, job, "run_meta.multi_failed.json"))
            status2 = attempt(job, False, t_single)
            results[job] = f"multi:{status} single:{status2}"
        else:
            results[job] = "ok"
    log(f"BATCH COMPLETE in {round((time.time()-t0)/3600, 2)}h: {json.dumps(results)}")
    with open(os.path.join(HERE, "batch_summary.json"), "w") as f:
        json.dump({"finished_utc": datetime.now(timezone.utc).isoformat(),
                   "results": results}, f, indent=2)

if __name__ == "__main__":
    main()
