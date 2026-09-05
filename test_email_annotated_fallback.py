"""Tests for the annotated-drawings email fallback (2026-09-05).

Every estimate email since the 2026-09 jobs silently lost its annotated
drawings: the files run 21-60 MB against a ~16 MB attachment budget, so the
size guard omitted them on every send (364 Main, MCC, Toyota, Bishop
Kearney, DePaul). The customer-facing fix: when the full annotated PDF
can't ride along, cut a marked-sheets-only email edition that fits, and
always link the job page where the full set lives.

Covered here:
  (1) _email_edition_of_annotated_pdf keeps only the marked sheets when the
      vector subset already fits.
  (2) An incompressible set falls through to rasterization and still fits.
  (3) No marked pages / no budget -> None, no file left behind.
  (4) _build_and_upload_annotated_drawings attaches the full file when it
      fits, or the marked-sheets edition (plus a body note) when it doesn't.
  (5) NIGHTSHIFT_EMAIL_ANNOTATED_FALLBACK=0 restores attach-or-omit.
  (6) send_result_email renders the job-page link and upstream body notes.

Offline, no API, no network.
"""
import os
import sys
import tempfile

import fitz

import jobs

_fails = []
MB = 1024 * 1024


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


def _noise_pdf(path, pages=6, big=False):
    """A synthetic plan set. big=True embeds random-noise images so the file
    is large AND incompressible — the shape that defeats the vector subset."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=612, height=792)
        if big:
            # Random noise compresses to ~nothing under low-DPI JPEG but
            # stays heavy as an embedded lossless image. P6 PPM keeps this
            # dependency-free.
            w, h = 1400, 1800
            ppm = b"P6\n%d %d\n255\n" % (w, h) + os.urandom(w * h * 3)
            page.insert_image(page.rect, stream=ppm)
        else:
            page.insert_text((72, 72), "sheet")
    doc.save(path)
    doc.close()


print("email edition core")
with tempfile.TemporaryDirectory() as wd:
    small_full = os.path.join(wd, "small.annotated.pdf")
    _noise_pdf(small_full, pages=6, big=False)

    out = os.path.join(wd, "small.edition.pdf")
    got = jobs._email_edition_of_annotated_pdf(small_full, [2, 5], out, 10 * MB)
    n_pages = fitz.open(got).page_count if got else 0
    check("(1) vector subset keeps only marked sheets",
          got == out and n_pages == 2, f"pages={n_pages}")

    big_full = os.path.join(wd, "big.annotated.pdf")
    _noise_pdf(big_full, pages=3, big=True)
    big_size = os.path.getsize(big_full)

    out2 = os.path.join(wd, "big.edition.pdf")
    # Budget below even a 1-page vector subset of noise, but comfortably
    # above a 75-dpi JPEG render of one letter sheet.
    got2 = jobs._email_edition_of_annotated_pdf(big_full, [2], out2, 1 * MB)
    check("(2) incompressible set rasterizes under budget",
          got2 == out2 and 0 < os.path.getsize(out2) <= 1 * MB,
          f"full={big_size // 1024}KB edition={os.path.getsize(out2) // 1024 if got2 else 0}KB")

    check("(3a) no marked pages -> None",
          jobs._email_edition_of_annotated_pdf(small_full, [], out, 10 * MB) is None)
    out3 = os.path.join(wd, "never.pdf")
    check("(3b) impossible budget -> None, file cleaned up",
          jobs._email_edition_of_annotated_pdf(big_full, [1], out3, 1024) is None
          and not os.path.exists(out3))

print("build-and-upload wiring")


class _Stub:
    uploads = []


def _fake_upload(path, key, content_type=None):
    _Stub.uploads.append(os.path.basename(path))


def _fake_record(*a, **k):
    pass


with tempfile.TemporaryDirectory() as wd:
    src = os.path.join(wd, "plans.pdf")
    _noise_pdf(src, pages=3, big=True)
    small_src = os.path.join(wd, "smallplans.pdf")
    _noise_pdf(small_src, pages=3, big=False)

    result = {"analysis": {"floors": [{"rooms": [
        {"bbox": {"source_pdf": src}, "source_page": 2},
    ]}]}}
    small_result = {"analysis": {"floors": [{"rooms": [
        {"bbox": {"source_pdf": small_src}, "source_page": 2},
    ]}]}}

    import bbox_spike
    old_render = bbox_spike.render_annotated_pdf
    old_upload, old_record = jobs.storage.upload_file, jobs._record_result_file

    def _fake_render(pdf_in, res, pdf_out):
        import shutil
        shutil.copyfile(pdf_in, pdf_out)
        return {"pages": 3, "referenced_pages": 1, "rooms_drawn": 1,
                "misses_marked": 0, "extraction_failures": 0,
                "marked_page_numbers": [2],
                "output_size_bytes": os.path.getsize(pdf_out)}

    try:
        bbox_spike.render_annotated_pdf = _fake_render
        jobs.storage.upload_file = _fake_upload
        jobs._record_result_file = _fake_record

        # Full file fits: reserved leaves plenty of room.
        paths, notes = jobs._build_and_upload_annotated_drawings(
            "sub-1", small_result, [small_src], wd, reserved_bytes=0)
        check("(4a) full file attached when it fits",
              [os.path.basename(p) for p in paths]
              == ["smallplans.annotated.pdf"] and not notes)

        # Reserve nearly the whole budget so the full file can't fit but a
        # rasterized marked-sheets edition can.
        reserved = jobs._ATTACH_BUDGET_RAW - 1 * MB
        paths, notes = jobs._build_and_upload_annotated_drawings(
            "sub-2", result, [src], wd, reserved_bytes=reserved)
        check("(4b) marked-sheets edition attached when full is over budget",
              [os.path.basename(p) for p in paths]
              == ["plans.annotated.marked-sheets.pdf"],
              str([os.path.basename(p) for p in paths]))
        check("(4c) body note points at the job page",
              len(notes) == 1 and "marked-up" in notes[0]
              and "job page" in notes[0], str(notes))
        check("(4d) full-resolution file still uploaded to R2",
              _Stub.uploads.count("plans.annotated.pdf") >= 1
              and "plans.annotated.marked-sheets.pdf" not in _Stub.uploads)

        os.environ["NIGHTSHIFT_EMAIL_ANNOTATED_FALLBACK"] = "0"
        try:
            paths, notes = jobs._build_and_upload_annotated_drawings(
                "sub-3", result, [src], wd, reserved_bytes=reserved)
            check("(5) kill switch restores attach-or-omit",
                  [os.path.basename(p) for p in paths]
                  == ["plans.annotated.pdf"] and not notes)
        finally:
            os.environ.pop("NIGHTSHIFT_EMAIL_ANNOTATED_FALLBACK", None)
    finally:
        bbox_spike.render_annotated_pdf = old_render
        jobs.storage.upload_file = old_upload
        jobs._record_result_file = old_record

print("send_result_email body")


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


with tempfile.TemporaryDirectory() as wd:
    analysis_pdf = os.path.join(wd, "analysis.pdf")
    with open(analysis_pdf, "wb") as f:
        f.write(b"%PDF-1.4 stub")

    contact = {"name": "Test", "email": "t@example.com"}
    result = {"cost_estimate": {"subtotal": 1, "line_items": []},
              "analysis": {}, "output_pdf_path": analysis_pdf}

    old_addr, old_pw = jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD
    old_smtp = jobs.smtplib.SMTP
    jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = "t@x.com", "pw"
    try:
        jobs.smtplib.SMTP = _CaptureSMTP
        jobs.send_result_email(
            contact, result, submission_id="abc-123",
            extra_body_notes=["plans.annotated.marked-sheets.pdf contains "
                              "the marked-up sheets only."])
        body = _CaptureSMTP.sent[-1].get_payload()[0].get_payload()
        check("(6a) job-page link in body", "/jobs/abc-123" in body)
        check("(6b) upstream note in body", "marked-up sheets only" in body)

        _CaptureSMTP.sent = []
        jobs.send_result_email(contact, result)
        body = _CaptureSMTP.sent[-1].get_payload()[0].get_payload()
        check("(6c) no link line without a submission id",
              "/jobs/" not in body)
    finally:
        jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = old_addr, old_pw
        jobs.smtplib.SMTP = old_smtp

print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED'} ===")
sys.exit(1 if _fails else 0)
