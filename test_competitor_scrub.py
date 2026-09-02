#!/usr/bin/env python3
"""No competitor name reaches customer-facing output, however it got there.

Companion to test_whitelabel_contractor.py. Binding the persona stops the
model WRITING "Rider Painting"; this catches a name that reaches the model
some other way — a drawing that names a competitor, a scope note pasted
from a prior bid — and would otherwise ride out into a document the
customer hands to a GC. The 2026-09-01 Profeta delivery shipped 11 such
references.

Locks in: (1) prose, RFI, exclusion and confidence text are all scrubbed,
(2) the job's OWN name is never scrubbed, (3) possessive forms are handled,
(4) a hit is recorded rather than silently laundered, (5) the flag OFF is
fully inert.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDE_API_KEY", "x")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


def load(flag):
    os.environ["NIGHTSHIFT_COMPETITOR_SCRUB"] = flag
    sys.modules.pop("will_synthesis", None)
    import will_synthesis as W
    return W


def sample():
    return {
        "gc_scope_of_work": "Rider Painting will prime and finish all GWB. "
                            "— Will, Senior Estimator, Rider Painting, Inc.",
        "estimator_recap": "Touch-up of Rider's own work is included.",
        "additional_rfis": [{"question": "Confirm whether Rider Painting "
                                         "should price Alternate No. 1."}],
        "additional_exclusions": [{"reason": "Not field-painted by Rider "
                                             "Painting."}],
        "confidence": {"reasoning": "Rider Painting has bid this scope.",
                       "top_risks": ["Rider Painting scope unclear"]},
    }


W = load("1")

def _customer_facing(d):
    """Everything except the audit record, which NAMES what it replaced."""
    return str({k: v for k, v in d.items() if k != "_competitor_scrub"})


out = W.scrub_competitor_names(sample(), "Profeta Painting")
check("Rider" not in _customer_facing(out),
      f"a competitor name survived the scrub: {_customer_facing(out)[:150]}")
check("Profeta Painting" in out["gc_scope_of_work"],
      "scrub did not substitute the job's own contractor")
check("Profeta Painting's own work" in out["estimator_recap"],
      f"possessive form mishandled: {out['estimator_recap']}")
check("Rider" not in str(out["additional_rfis"]), "RFI text not scrubbed")
check("Rider" not in str(out["additional_exclusions"]),
      "exclusion text not scrubbed")
check("Rider" not in str(out["confidence"]),
      "confidence text (incl. list fields) not scrubbed")
check(out.get("_competitor_scrub", {}).get("with") == "Profeta Painting",
      "the scrub did not record what it replaced")

# Unknown contractor -> neutral, still never the competitor.
n = W.scrub_competitor_names(sample(), None)
check("Rider" not in _customer_facing(n),
      "competitor survived when contractor unknown")
check("the Contractor" in n["gc_scope_of_work"],
      "neutral replacement not applied")

# The job's OWN name must never be scrubbed out of its own document.
own = W.scrub_competitor_names(
    {"gc_scope_of_work": "Rider Painting will prime all GWB."},
    "Rider Painting")
check(own["gc_scope_of_work"] == "Rider Painting will prime all GWB.",
      f"scrubbed the contractor's own name: {own['gc_scope_of_work']}")
check("_competitor_scrub" not in own, "recorded a hit on the owner's own name")

# Clean output is untouched and unflagged.
clean = W.scrub_competitor_names(
    {"gc_scope_of_work": "Profeta Painting will prime all GWB."},
    "Profeta Painting")
check("_competitor_scrub" not in clean, "flagged a scrub on clean output")

# Flag OFF is fully inert.
off = load("0").scrub_competitor_names(sample(), "Profeta Painting")
check("Rider Painting" in off["gc_scope_of_work"],
      "scrub ran with the flag OFF")

sys.modules.pop("will_synthesis", None)
os.environ.pop("NIGHTSHIFT_COMPETITOR_SCRUB", None)
import will_synthesis as W
check(W._competitor_scrub_enabled(), "scrub is not ON by default")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
