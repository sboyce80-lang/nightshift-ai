"""Regression tests for the broken-PDF filtered-copy crash (2026-07-06).

Scott Redmond (Devine) submitted the PNC WI Milwaukee CD set — a PDF with
dangling indirect references ("Object 1419 0 not defined"). Room extraction
succeeded (182 rooms), then the window-schedule re-analysis pass called
_create_filtered_pdf, where PyPDF2's page clone raised a bare AssertionError.
That killed the whole job, and because str(AssertionError()) == "", the
customer error email, the DB error column, and the log line were all blank.

Three fixes covered here:
  (1) _create_filtered_pdf falls back to PyMuPDF when PyPDF2 can't clone
      the pages (same page set, so the filtered→original index map holds).
  (2) jobs._describe_exc never returns an empty string for message-less
      exceptions.
  (3) send_error_email substitutes a generic line for a blank error_msg
      (body-construction level; SMTP is not exercised offline).

Offline, no API.
"""
import io

import fitz
import PyPDF2

import Takeoff_DIRECT as T
import jobs

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _make_pdf(path, n_pages=4):
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"page {i}")
    doc.save(path)
    doc.close()


# ── Fix 1: PyMuPDF fallback when PyPDF2 cannot clone pages ──────────────────
print("\nFix 1 — _create_filtered_pdf survives PyPDF2 clone failures")
import tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
tmp.close()
_make_pdf(tmp.name)

# Sanity: normal path still uses PyPDF2 and works.
data = T._create_filtered_pdf(tmp.name, [0, 2])
check(len(PyPDF2.PdfReader(io.BytesIO(data)).pages) == 2,
      "healthy PDF: PyPDF2 path returns the 2 selected pages")

# Simulate the PNC failure: PyPDF2's add_page raises a bare AssertionError.
_orig_add_page = PyPDF2.PdfWriter.add_page
def _boom(self, page, *a, **kw):
    raise AssertionError
PyPDF2.PdfWriter.add_page = _boom
try:
    data = T._create_filtered_pdf(tmp.name, [0, 2])
finally:
    PyPDF2.PdfWriter.add_page = _orig_add_page

check(bool(data), "broken clone: fallback still returns bytes")
out = fitz.open(stream=data, filetype="pdf")
check(len(out) == 2, "fallback PDF has exactly the 2 selected pages")
check("page 0" in out[0].get_text() and "page 2" in out[1].get_text(),
      "fallback preserves page order (index map stays valid)")
out.close()
os.unlink(tmp.name)

# Real reproduction (no monkeypatch): a dangling indirect reference makes
# PyPDF2's clone raise a bare AssertionError, exactly like the PNC set.
tmp2 = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
tmp2.close()
doc = fitz.open()
for i in range(3):
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), f"page {i}")
doc.xref_set_key(doc[1].xref, "Annots", "[1419 0 R]")  # Object 1419 0 not defined
doc.save(tmp2.name)
doc.close()

pypdf2_crashed = False
try:
    _r = PyPDF2.PdfReader(tmp2.name)
    _w = PyPDF2.PdfWriter()
    for i in range(3):
        _w.add_page(_r.pages[i])
except AssertionError:
    pypdf2_crashed = True
check(pypdf2_crashed, "dangling-ref PDF reproduces PyPDF2's bare AssertionError")

data = T._create_filtered_pdf(tmp2.name, [0, 2])
out = fitz.open(stream=data, filetype="pdf")
check(len(out) == 2 and "page 2" in out[1].get_text(),
      "dangling-ref PDF: fixed function still returns the selected pages")
out.close()
os.unlink(tmp2.name)

# ── Fix 2: message-less exceptions never yield a blank description ──────────
print("\nFix 2 — _describe_exc")
check(jobs._describe_exc(AssertionError()) == "Unexpected error (AssertionError)",
      "bare AssertionError -> named placeholder, not ''")
check(jobs._describe_exc(ValueError("boom")) == "boom",
      "real message passes through untouched")
check(jobs._describe_exc(RuntimeError("  ")) == "Unexpected error (RuntimeError)",
      "whitespace-only message treated as blank")

# ── Fix 3: send_error_email backstop for blank error_msg ────────────────────
print("\nFix 3 — send_error_email blank-message backstop")
sent_bodies = []


class _FakeSMTP:
    def __init__(self, *a, **kw):
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
    def send_message(self, msg):
        sent_bodies.append(msg.get_payload(0).get_payload())


_orig_smtp = jobs.smtplib.SMTP
_orig_addr, _orig_pw = jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD
jobs.smtplib.SMTP = _FakeSMTP
jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = "test@example.com", "x"
try:
    jobs.send_error_email({"name": "Scott", "email": "scott@example.com"}, "")
finally:
    jobs.smtplib.SMTP = _orig_smtp
    jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = _orig_addr, _orig_pw

check(len(sent_bodies) == 1, "email sent through fake SMTP")
body = sent_bodies[0] if sent_bodies else ""
check("An unexpected error occurred" in body,
      "blank error_msg replaced with generic sentence")
check("documents:\n  \n" not in body,
      "no blank line where the error message belongs")


print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
