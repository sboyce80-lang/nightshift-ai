#!/usr/bin/env python3
"""Single source of truth for the production flag set.

Every validation harness before this carried its own hand-rolled FLAGS dict:
run_one.py had 9, rerun_batch.sh had 29, the 2026-09-04 golden replay had 38.
Production runs 64. So no batch comparison made against any of them was
measuring what production does, and a "regression" found that way is
indistinguishable from a configuration difference. On 2026-09-04 that cost a
full 8-job replay, a re-run of it, and a 5-point bisect before the baseline
itself turned out to be the variable.

Usage:
    from golden.load_prod_flags import apply_prod_flags
    applied = apply_prod_flags()          # sets os.environ, returns the dict

Capture the file from Render: dashboard -> Env Groups (or a worker's
Environment tab) -> copy every NIGHTSHIFT_* row. Secrets are deliberately
excluded — this file holds behaviour flags only and nothing credentialed.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROD_FLAGS_PATH = os.path.join(HERE, "prod_flags.env")


def read_prod_flags(path=PROD_FLAGS_PATH):
    """Parse prod_flags.env -> {name: value}. Raises if it is missing:
    silently falling back to code defaults is how a harness drifts."""
    flags = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if not k.startswith("NIGHTSHIFT_"):
                # Nothing credentialed belongs here.
                continue
            flags[k] = v.strip()
    return flags


def apply_prod_flags(path=PROD_FLAGS_PATH, override=True):
    """Set the production flag set into os.environ.

    override=True by default: a harness that half-inherits the ambient shell
    is exactly the drift this module exists to stop.
    """
    flags = read_prod_flags(path)
    for k, v in flags.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return flags


def assert_no_extra_nightshift_flags(allow=()):
    """Fail loudly if the environment carries a NIGHTSHIFT_* flag that
    production does not define. That is how run 1 of the golden replay set 38
    flags and could attribute nothing."""
    known = set(read_prod_flags()) | set(allow)
    extra = sorted(k for k in os.environ
                   if k.startswith("NIGHTSHIFT_") and k not in known)
    if extra:
        raise AssertionError(
            "environment sets NIGHTSHIFT flags production does not define: "
            + ", ".join(extra))
    return True


if __name__ == "__main__":
    f = read_prod_flags()
    print(f"{len(f)} production flags")
    for k, v in sorted(f.items()):
        print(f"  {k}={v}")
