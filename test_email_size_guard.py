"""Tests for the estimate-email attachment size guard (2026-07-22).

Gmail rejects messages over 25 MB with SMTP 552. On 2026-07-21 three
estimate emails bounced this way (Wee Burn 192 MB, Otto 54 MB, Columbia
118 MB annotated PDFs) and, because send_result_email swallowed the SMTP
error, the email claim stayed held so nothing could retry.

Covered here:
  (1) _budget_attachments keeps everything when the total fits.
  (2) An oversized file is skipped while later smaller files still fit.
  (3) NIGHTSHIFT_EMAIL_SIZE_GUARD=0 kill switch restores attach-everything.
  (4) Missing/None paths are dropped silently.
  (5) send_result_email notes omitted files in the body and only attaches
      the kept ones (SMTP captured, not exercised).
  (6) SMTP failure re-raises so the caller can release the email claim.

Offline, no API, no network.
"""
import os
import sys
import tempfile

import jobs

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


def _mkfile(dirname, name, size):
    path = os.path.join(dirname, name)
    with open(path, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")
    return path


def with_flag(val, fn):
    old = os.environ.get("NIGHTSHIFT_EMAIL_SIZE_GUARD")
    if val is None:
        os.environ.pop("NIGHTSHIFT_EMAIL_SIZE_GUARD", None)
    else:
        os.environ["NIGHTSHIFT_EMAIL_SIZE_GUARD"] = val
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("NIGHTSHIFT_EMAIL_SIZE_GUARD", None)
        else:
            os.environ["NIGHTSHIFT_EMAIL_SIZE_GUARD"] = old


MB = 1024 * 1024

print("attachment budget core")
with tempfile.TemporaryDirectory() as wd:
    small_a = _mkfile(wd, "analysis.pdf", 200 * 1024)
    small_b = _mkfile(wd, "estimate.pdf", 100 * 1024)
    huge = _mkfile(wd, "annotated.pdf", 120 * MB)  # sparse — never read
    small_c = _mkfile(wd, "annotated_page.pdf", 1 * MB)

    kept, skipped = jobs._budget_attachments([small_a, small_b])
    check("(1) small set all kept", kept == [small_a, small_b] and not skipped)

    kept, skipped = jobs._budget_attachments([small_a, small_b, huge, small_c])
    check("(2) oversized skipped, later small kept",
          kept == [small_a, small_b, small_c] and skipped == [huge],
          f"kept={[os.path.basename(p) for p in kept]}")

    kept, skipped = with_flag(
        "0", lambda: jobs._budget_attachments([small_a, huge]))
    check("(3) kill switch attaches everything",
          kept == [small_a, huge] and not skipped)

    kept, skipped = jobs._budget_attachments(
        [None, os.path.join(wd, "nope.pdf"), small_a])
    check("(4) missing/None dropped silently",
          kept == [small_a] and not skipped)

    print("send_result_email wiring")

    class _CaptureSMTP:
        sent = []

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg, **k):
            _CaptureSMTP.sent.append(msg)

    class _BoomSMTP(_CaptureSMTP):
        def send_message(self, msg, **k):
            raise RuntimeError("552 size limit")

    contact = {"name": "Test", "email": "t@example.com"}
    result = {"cost_estimate": {"subtotal": 1, "line_items": []},
              "analysis": {}, "output_pdf_path": small_a}

    old_addr, old_pw = jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD
    old_smtp = jobs.smtplib.SMTP
    jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = "t@x.com", "pw"
    try:
        jobs.smtplib.SMTP = _CaptureSMTP
        jobs.send_result_email(contact, result,
                               extra_attachment_paths=[huge, small_c],
                               estimate_pdf_path=small_b)
        msg = _CaptureSMTP.sent[-1]
        attached = [p.get_filename() for p in msg.get_payload()
                    if p.get_filename()]
        body = msg.get_payload()[0].get_payload()
        check("(5a) only kept files attached",
              attached == ["analysis.pdf", "estimate.pdf",
                           "annotated_page.pdf"], str(attached))
        check("(5b) body notes omitted file",
              "too large to attach" in body and "annotated.pdf" in body)

        _CaptureSMTP.sent = []
        jobs.send_result_email(contact, result,
                               estimate_pdf_path=small_b)
        body = _CaptureSMTP.sent[-1].get_payload()[0].get_payload()
        check("(5c) no note when nothing omitted",
              "too large to attach" not in body)

        jobs.smtplib.SMTP = _BoomSMTP
        try:
            jobs.send_result_email(contact, result,
                                   estimate_pdf_path=small_b)
            raised = False
        except RuntimeError:
            raised = True
        check("(6) SMTP failure re-raises for claim release", raised)
    finally:
        jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = old_addr, old_pw
        jobs.smtplib.SMTP = old_smtp

print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED'} ===")
sys.exit(1 if _fails else 0)
