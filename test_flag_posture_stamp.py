#!/usr/bin/env python3
"""Worker-side flag posture stamping (jobs._stamp_flag_posture).

The resolver decides a job's conventions; this is the half that makes the
decision auditable and safe. Locks in: the posture + per-flag provenance
land on the result AND on the uploaded JSON; unconfirmed conventions
append RFIs without clobbering the extractor's own; the hold reason is
appended to an existing one rather than replacing it; SHADOW mode records
the posture but never adds an RFI or holds a job; and a stamping failure
can never cost a customer a finished estimate."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobs as J  # noqa: E402
import flag_resolver as FR  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def _result(tmpdir, **extra):
    path = os.path.join(tmpdir, "result.json")
    result = {
        "analysis": {"notes": ["[Coverage] 12 pages measured."],
                     "rfi_items": [{"category": "Incomplete Dimensions",
                                    "question": "Ceiling height on A-201?"}]},
        "cost_estimate": {"subtotal": 41250.0},
        "rfi_items": [{"category": "Incomplete Dimensions",
                       "question": "Ceiling height on A-201?"}],
        "output_json_path": path,
    }
    result.update(extra)
    with open(path, "w") as fh:
        json.dump(result, fh)
    return result


def _resolution(enabled):
    os.environ["NIGHTSHIFT_FLAG_RESOLVER"] = "1" if enabled else "0"
    return FR.resolve_flags(profile=None, org_label="Brand New Co")


print("1) Enabled: posture recorded, RFIs appended, job held")
with tempfile.TemporaryDirectory() as tmp:
    res = _result(tmp)
    resolution = _resolution(True)
    J._stamp_flag_posture("sub-1", res, resolution)

    check(res["flag_posture"]["enabled"] is True,
          "the posture is recorded on the result")
    check(res["flag_posture"]["provenance"]["NIGHTSHIFT_JOB_DRAW_MEDIAN"]
          == "engine",
          "per-flag provenance travels with the posture")
    check(res["analysis"]["flag_posture"] == res["flag_posture"],
          "the analysis block carries the same posture")
    check(res["manual_review_required"] is True, "the job is held")
    check("Brand New Co" in res["manual_review_reason"],
          "the hold reason names the customer")

    questions = [i["question"] for i in res["rfi_items"]]
    check("Ceiling height on A-201?" in questions,
          "the extractor's own RFIs survive")
    check(len(res["rfi_items"]) == 1 + len(FR.CONVENTION_FLAGS),
          "convention RFIs are appended, not substituted")
    check(len(res["analysis"]["rfi_items"]) == 1 + len(FR.CONVENTION_FLAGS),
          "the analysis RFI list is appended too")

    with open(res["output_json_path"]) as fh:
        on_disk = json.load(fh)
    check(on_disk["flag_posture"] == res["flag_posture"],
          "the UPLOADED json carries the posture, not just the in-memory result")
    check(on_disk["manual_review_required"] is True,
          "the uploaded json carries the hold")
    check(len(on_disk["rfi_items"]) == 1 + len(FR.CONVENTION_FLAGS),
          "the uploaded json carries the convention RFIs")

print("2) An existing hold reason is appended to, never replaced")
with tempfile.TemporaryDirectory() as tmp:
    res = _result(tmp, manual_review_required=True,
                  manual_review_reason="4 page(s) could not be analyzed")
    J._stamp_flag_posture("sub-2", res, _resolution(True))
    check("4 page(s) could not be analyzed" in res["manual_review_reason"],
          "the extractor's reason survives")
    check("unconfirmed" in res["manual_review_reason"],
          "the convention reason is appended alongside it")

print("3) Shadow mode records the posture and changes nothing else")
with tempfile.TemporaryDirectory() as tmp:
    res = _result(tmp)
    J._stamp_flag_posture("sub-3", res, _resolution(False))
    check(res["flag_posture"]["enabled"] is False,
          "the posture is still recorded in shadow")
    check(res["flag_posture"]["unresolved"],
          "shadow still reports what it would have asked about")
    check("manual_review_required" not in res,
          "shadow never holds a job")
    check(len(res["rfi_items"]) == 1,
          "shadow never adds an RFI to a customer's estimate")

print("4) No resolution (legacy enqueue) is a clean no-op")
with tempfile.TemporaryDirectory() as tmp:
    res = _result(tmp)
    before = json.dumps(res, sort_keys=True)
    J._stamp_flag_posture("sub-4", res, None)
    check(json.dumps(res, sort_keys=True) == before,
          "a job enqueued before the resolver is untouched")

print("5) A stamping failure never costs the customer an estimate")
with tempfile.TemporaryDirectory() as tmp:
    res = _result(tmp)
    res["output_json_path"] = os.path.join(tmp, "does-not-exist.json")
    J._stamp_flag_posture("sub-5", res, _resolution(True))
    check(res["cost_estimate"]["subtotal"] == 41250.0,
          "a missing json path does not raise or damage the result")

    class Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    res2 = _result(tmp)
    J._stamp_flag_posture("sub-6", res2, Exploding(enabled=True))
    check(res2["cost_estimate"]["subtotal"] == 41250.0,
          "a resolver bug is swallowed, not propagated")

os.environ.pop("NIGHTSHIFT_FLAG_RESOLVER", None)
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all flag posture stamping checks passed")
