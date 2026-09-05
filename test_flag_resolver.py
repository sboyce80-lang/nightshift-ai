#!/usr/bin/env python3
"""Per-job flag resolver (NIGHTSHIFT_FLAG_RESOLVER).

The 9/1 Caris prod-posture smoke test came in at -44.9% against a +2.6%
banded run of the same code: the band depended on WALL_BASIS_FACES, a
fact about ONE customer, which is correctly off in prod. Process-wide
env can hold exactly one customer's conventions, so every
convention-dependent banded result was unreachable in production.

Locks in: layer precedence (engine < profile < estimate < evidence);
an unconfirmed convention is an open question, never a silent "no";
unknown customer gets conservative defaults + RFI + hold; a
reviewer-confirmed profile stops RFI'ing; flag off resolves but steers
nothing; and the resolver reproduces the K=3 harness postures for the
three customers whose bands depended on them."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import flag_resolver as FR  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def _on():
    os.environ["NIGHTSHIFT_FLAG_RESOLVER"] = "1"


def _off():
    os.environ.pop("NIGHTSHIFT_FLAG_RESOLVER", None)


CONFIRMED = "2026-09-01T00:00:00+00:00"


def _complete(**answers):
    """A reviewer-confirmed profile: the answers given, rest = defaults."""
    return FR.confirm_profile(None, answers, CONFIRMED)


print("1) Layer precedence: engine < profile < estimate < evidence")
_on()
r = FR.resolve_flags(profile=_complete())
check(r["flags"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "0"
      and r["provenance"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "profile",
      "confirmed-but-silent profile resolves to the engine default")

r = FR.resolve_flags(profile=_complete(wall_basis_faces="1"))
check(r["flags"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "1"
      and r["provenance"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "profile",
      "profile overrides the engine default")

r = FR.resolve_flags(profile=_complete(wall_basis_faces="1"),
                     overrides={"wall_basis_faces": "0"})
check(r["flags"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "0"
      and r["provenance"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "estimate",
      "this estimate overrides the profile")

r = FR.resolve_flags(profile=_complete(wall_basis_faces="1"),
                     overrides={"wall_basis_faces": "1"},
                     evidence={"wall_basis_faces": ("0", "plans note runs")})
check(r["flags"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "0"
      and r["provenance"]["NIGHTSHIFT_WALL_BASIS_FACES"] == "evidence",
      "plan evidence overrides every stated convention")

print("2) An unconfirmed convention is an open question, not a 'no'")
r = FR.resolve_flags(profile={"interior_only": "1"})  # no confirmed marker
check("wall_basis_faces" in r["unresolved"],
      "a key absent from an unconfirmed profile stays unresolved")
check("interior_only" not in r["unresolved"],
      "a key present in the profile is resolved")
for blank in (None, "", "unknown", "?"):
    r = FR.resolve_flags(profile={"interior_only": blank})
    check("interior_only" in r["unresolved"],
          f"blank value {blank!r} must not answer the question")

print("3) Unknown customer: conservative defaults + RFI + hold")
r = FR.resolve_flags(profile=None, org_label="Brand New Co")
check(len(r["unresolved"]) == len(FR.CONVENTION_FLAGS),
      "no profile leaves every convention unresolved")
check(all(r["flags"][c.name] == c.default for c in FR.CONVENTION_FLAGS),
      "unresolved conventions sit at the conservative engine default")
check(r["manual_review"] is True, "unknown customer is held for review")
check(len(r["rfi_items"]) == len(FR.CONVENTION_FLAGS),
      "every unresolved convention gets its own RFI")
check(all(i["category"] == "Bidding Convention" for i in r["rfi_items"]),
      "RFIs are categorised so the estimate PDF can group them")
check("Brand New Co" in r["review_reason"],
      "the hold reason names the customer")

print("4) Reviewer write-back ends the RFI loop")
r_before = FR.resolve_flags(profile=None, org_label="Dutchess Livestock")
profile = FR.confirm_profile(None, {"interior_only": "1"}, CONFIRMED)
r_after = FR.resolve_flags(profile=profile, org_label="Dutchess Livestock")
check(r_before["manual_review"] and not r_after["manual_review"],
      "a confirmed profile clears the hold")
check(r_after["rfi_items"] == [], "a confirmed profile raises no RFI")
check(r_after["flags"]["NIGHTSHIFT_INTERIOR_ONLY_CONVENTION"] == "1",
      "the confirmed answer is what the next job runs")

partial = FR.confirm_profile(None, {"interior_only": "1"}, CONFIRMED,
                             complete=False)
r_partial = FR.resolve_flags(profile=partial)
check(r_partial["manual_review"] is True,
      "a PARTIAL confirmation keeps the remaining questions open")
check(r_partial["flags"]["NIGHTSHIFT_INTERIOR_ONLY_CONVENTION"] == "1",
      "a partial confirmation still applies what was answered")

check(FR.confirm_profile({"interior_only": "1"}, {"bogus_key": "1"},
                         CONFIRMED).get("bogus_key") is None,
      "write-back ignores keys outside the convention registry")

original = {"interior_only": "1"}
FR.confirm_profile(original, {"wall_basis_faces": "1"}, CONFIRMED)
check(original == {"interior_only": "1"},
      "write-back never mutates the caller's profile")

print("5) Flag off resolves but steers nothing")
_off()
r = FR.resolve_flags(profile=None)
check(r["enabled"] is False, "resolver reports disabled")
check(r["unresolved"] and r["flags"],
      "shadow mode still produces a full posture to record")
env = {}
check(FR.apply_flags(r, env) == {} and env == {},
      "a disabled resolution touches no environment variable")

_on()
env = {"NIGHTSHIFT_VME_PRIMARY": "1"}
r = FR.resolve_flags(profile=_complete(wall_basis_faces="1"))
changed = FR.apply_flags(r, env)
check(env["NIGHTSHIFT_WALL_BASIS_FACES"] == "1",
      "an enabled resolution writes its flags into the environment")
check("NIGHTSHIFT_VME_PRIMARY" not in changed,
      "flags already at the resolved value are not reported as changed")

print("6) Measurement ladder is pinned and separate from conventions")
r = FR.resolve_flags(profile=_complete())
for name, value in FR.MEASUREMENT_FLAGS.items():
    check(r["flags"][name] == value and r["provenance"][name] == "engine",
          f"{name} is pinned by the engine layer")
check(not (set(FR.MEASUREMENT_FLAGS) & set(FR.CONVENTION_BY_NAME)),
      "no flag is both a measurement flag and a convention")
check(r["flags"]["NIGHTSHIFT_JOB_DRAW_MEDIAN"] == "3",
      "the variance program travels with every job")
check(set(r["conventions"]) == {c.key for c in FR.CONVENTION_FLAGS},
      "the conventions block covers exactly the registry")

print("7) K=3 harness postures reproduce from a customer profile")
# The three customers whose banded results depended on a convention.
# Source: nsai_k3_marathon_2026-08-25/run_k3_child.py JOBS extra_env.
K3 = {
    "Caris Hyde Park": ({"wall_basis_faces": "1"},
                        {"NIGHTSHIFT_WALL_BASIS_FACES": "1"}),
    "Dutchess Livestock": ({"interior_only": "1"},
                           {"NIGHTSHIFT_INTERIOR_ONLY_CONVENTION": "1"}),
    "364 Main": ({"interior_only": "1"},
                 {"NIGHTSHIFT_INTERIOR_ONLY_CONVENTION": "1"}),
    "Honey Farms Malta": ({"schedule_scope": "1"},
                          {"NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE": "1"}),
    # Fishkill's bid INCLUDES exterior — the counterexample that makes
    # interior-only per-customer rather than a class default.
    "397 Fishkill": ({}, {"NIGHTSHIFT_INTERIOR_ONLY_CONVENTION": "0"}),
}
for customer, (answers, expected) in K3.items():
    r = FR.resolve_flags(profile=_complete(**answers), org_label=customer)
    check(all(r["flags"][k] == v for k, v in expected.items()),
          f"{customer}: harness posture reproduces from the profile")
    check(not r["manual_review"],
          f"{customer}: a known customer is not held")

# The failure this build exists to prevent: two customers, one worker.
caris = FR.resolve_flags(profile=_complete(wall_basis_faces="1"))
harlem = FR.resolve_flags(profile=_complete())
check(caris["flags"]["NIGHTSHIFT_WALL_BASIS_FACES"] !=
      harlem["flags"]["NIGHTSHIFT_WALL_BASIS_FACES"],
      "two customers resolve to opposite wall bases in one process")

_off()
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all flag resolver checks passed")
