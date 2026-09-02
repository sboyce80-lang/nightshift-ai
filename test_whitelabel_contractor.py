#!/usr/bin/env python3
"""Customer-facing copy must never name a contractor other than its owner.

2026-09-02, Profeta Painting (the first PLG self-serve customer). The Will
system prompt opened "You are Will, Senior Estimator for Rider Painting,
Inc." and instructed Will to sign gc_scope_of_work "— Will, Senior
Estimator, Rider Painting, Inc.". Three standard exclusions were hardcoded
the same way ("Rider Painting paints to a clean, prepared substrate", "Rider
does not perform abatement", "Touch-up of Rider's own work is included").

Anthony's 2026-09-01 delivery carried 11 "Rider" references, including a
GC-facing scope narrative signed in a competitor's name. Rider Painting is
a real and different customer, so this disclosed one customer's identity to
another on a document written to be handed to a general contractor.

Locks in: (1) the persona binds to the job's own contractor, (2) an
unresolved contractor goes NEUTRAL rather than borrowing a name,
(3) free-mail and blank senders never yield a company name, (4) no
hardcoded competitor survives in customer-facing copy.
"""
import os
import re
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


import will_synthesis as W
import Takeoff_DIRECT as T

# --- 1) The persona binds to whoever owns the job.
p = W.build_will_system_prompt("Profeta Painting")
check("Profeta Painting" in p, "contractor name absent from the bound prompt")
check("Rider" not in p, "a competitor name survived in the bound prompt")
check("— Will, Senior Estimator, Profeta Painting" in p,
      "signature not bound to the job's contractor")
check("__CONTRACTOR__" not in p and "__SIGNATURE__" not in p,
      "an unsubstituted placeholder leaked into the prompt")

# --- 2) Unknown contractor goes neutral, never borrowed.
n = W.build_will_system_prompt(None)
check("Rider" not in n, "competitor name reappeared in the neutral prompt")
check(W.NEUTRAL_SIGNATURE in n, "neutral prompt lost its unsigned signature")
check("__CONTRACTOR__" not in n and "__SIGNATURE__" not in n,
      "unsubstituted placeholder in the neutral prompt")
for blank in ("", "   "):
    check("Rider" not in W.build_will_system_prompt(blank),
          f"blank contractor {blank!r} did not go neutral")

# --- 3) Domain resolution.
check(T._resolve_contractor_name("anthony@profetapainting.com") == "Profetapainting",
      "company domain did not resolve to a name")
check(T._resolve_contractor_name("elliott@riderpaintingny.com") == "Rider Painting",
      "known-domain override lost")
check(T._resolve_contractor_name("someone@gmail.com") == "",
      "free-mail domain wrongly yielded a company name")
check(T._resolve_contractor_name("") == "", "blank email yielded a name")
check(T._resolve_contractor_name("not-an-email") == "",
      "malformed address yielded a name")
check(T._resolve_contractor_name("x@gmail.com", "Profeta Painting")
      == "Profeta Painting", "explicit business name was ignored")
check(T._resolve_contractor_name("a@profetapainting.com", "Profeta Painting")
      == "Profeta Painting",
      "business name did not beat the domain guess")

# --- 3b) Exclusion copy must be GRAMMATICAL in every case. "We paints to a
#     clean, prepared substrate" shipped in the first cut of this fix.
for name, subj, poss in (("Profeta Painting", "Profeta Painting",
                          "Profeta Painting's"),
                         ("", "The Contractor", "our"),
                         ("Acme Coatings", "Acme Coatings", "Acme Coatings'")):
    a = {"_contractor_name": name}
    T._ACTIVE_CONTRACTOR_NAME = ""
    check(T._contractor_subject(a) == subj,
          f"subject for {name!r} was {T._contractor_subject(a)!r}, want {subj!r}")
    check(T._contractor_possessive(a) == poss,
          f"possessive for {name!r} was {T._contractor_possessive(a)!r}, "
          f"want {poss!r}")

for name in ("Profeta Painting", ""):
    T._ACTIVE_CONTRACTOR_NAME = ""
    blob = " ".join(e.get("reason", "") for e in T._build_standard_exclusions(
        analysis={"_contractor_name": name}))
    check("We paints" not in blob and "Contractor paints to" in blob
          or "Painting paints to" in blob,
          f"ungrammatical exclusion copy for {name!r}")
    check(" own work" in blob and "of  own" not in blob,
          f"possessive dropped for {name!r}")

# --- 4) Standard exclusions carry no hardcoded competitor.
for analysis, expect in ((None, None), ({"_contractor_name": "Profeta Painting"},
                                        "Profeta Painting")):
    exc = T._build_standard_exclusions(analysis=analysis)
    blob = " ".join(f"{e.get('item','')} {e.get('reason','')}" for e in exc)
    check("Rider" not in blob,
          f"standard exclusions still name a competitor: "
          f"{[e for e in exc if 'Rider' in str(e)][:1]}")
    if expect:
        check(expect in blob, "exclusions did not adopt the contractor's name")

# --- 5) No competitor name survives in any customer-facing STRING.
#     Comments are fine; quoted copy is not.
for path in ("will_synthesis.py",):
    src = open(path).read()
    bad = [ln.strip() for ln in src.splitlines()
           if "Rider" in ln and not ln.lstrip().startswith("#")]
    check(not bad, f"{path} still has a competitor name outside a comment: "
                   f"{bad[:1]}")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
