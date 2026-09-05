#!/usr/bin/env python3
"""Delivery full package (NIGHTSHIFT_DELIVERY_FULL_PACKAGE, 2026-09-05).

Steven's product decision: every estimate delivery carries the full review
package — customer takeoff PDF, estimate PDF, annotated drawings, and a
"Please confirm scope" section (allowances, open RFIs, unpriced scope)
framed as: we provide the numbers, the customer decides what enters the
final bid.

Covered here, offline (no API, no SMTP, no WeasyPrint render):
  (1) Flag is default OFF, read at call time, house truthiness.
  (2) Flag off = today's behavior byte-identical: same body, same
      attachments, whether or not a takeoff path is offered.
  (3) The customer takeoff renders WITHOUT internal-voice content — a
      result poisoned with "Do NOT send this proposal", manual_review
      reasons and routing flags yields a document carrying none of it.
  (4) The white-label lesson: content that DOES carry internal voice or a
      competitor name (even via a room name) fails the scrub and
      generate_customer_takeoff_pdf refuses to produce a PDF.
  (5) Scope-confirm lists allowances (item/qty/amount/question), RFIs,
      and policy-uncertain lines — in the PDF section and the email body.
  (6) The email size guard applies to the takeoff attachment: an
      oversized takeoff is omitted with a note, never bounced.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDE_API_KEY", "x")
os.environ.pop("NIGHTSHIFT_DELIVERY_FULL_PACKAGE", None)

import jobs
import takeoff_customer_pdf as TC

_fails = []
MB = 1024 * 1024


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


def _org(name="Profeta Painting"):
    return SimpleNamespace(
        name=name, street_address="1 Main St", city="Rochester", state="NY",
        postal_code="14602", phone="555-0100",
        contact_email="office@example.com", website=None,
        logo_r2_key=None, logo_url=None, tax_id=None)


def _sub():
    return SimpleNamespace(id="abcd1234-0000-0000-0000-000000000000",
                           business_name="Anthony's GC", phone="",
                           scope_notes="")


def _poisoned_result():
    """The shape of a flagged run: reviewer dialect everywhere the
    internal report reads from, plus real measurements to render."""
    return {
        "manual_review_required": True,
        "manual_review_reason": ("Only 2 rooms found. Do NOT send this "
                                 "proposal without a reviewer sign-off."),
        "will_synthesis": {"pipeline_flags": {
            "ready_to_send": False, "route_to_human_review": True,
            "missing_information": ["Sheet A-301"]}},
        "analysis": {
            "manual_review_required": True,
            "manual_review_reason": "MANUAL REVIEW: coverage below gate.",
            "notes": ["internal gate record: manual_review forced",
                      "RFI REQUIRED: Confirm the wallcovering share."],
            "project_info": {"project_name": "168 Holley St",
                             "location": "Rochester, NY",
                             "total_floors_analyzed": 1,
                             "total_rooms_found": 2},
            "aggregated_totals": {
                "total_paintable_wall_sqft": 4210.5,
                "total_paintable_ceiling_sqft": 1200,
                "total_base_trim_lf": 350,
                "total_doors_full_paint": 6,
                "total_stair_sections": 2,
            },
            "floors": [{"floor_name": "First Floor", "rooms": [
                {"room_name": "Office 101",
                 "dimensions": {"length_feet": 12, "width_feet": 10,
                                "ceiling_height_feet": 9,
                                "wall_area_sqft": 396,
                                "ceiling_area_sqft": 120},
                 "elements": {"base_trim_lf": 44, "doors_full_paint": 1,
                              "windows_painted_interior": 2},
                 "notes": "manual_review: reviewer-only note"},
                {"room_name": "Corridor", "unit_multiplier": 4,
                 "dimensions": {"length_feet": 40, "width_feet": 6,
                                "wall_area_sqft": 828,
                                "ceiling_area_sqft": 240},
                 "elements": {"base_trim_lf": 92}},
            ]}],
        },
        "cost_estimate": {
            "subtotal": 21540.00,
            "line_items": [
                {"item": "Interior Walls - GYP", "qty": 4210.5,
                 "cost": 4631.55, "markup": 1389.47, "total": 6021.02},
                {"item": "Power Washing (ALLOWANCE — per plans note)",
                 "qty": 12000, "cost": 3600.0, "markup": 1080.0,
                 "total": 4680.00},
                {"item": "Level 5 Finish (ALLOWANCE — strike if excluded)",
                 "qty": 800, "cost": 960.0, "markup": 288.0,
                 "total": 1248.00},
                {"item": "Zeroed Allowance (ALLOWANCE)", "qty": 0,
                 "cost": 0, "markup": 0, "total": 0},
            ],
        },
        "rfi_items": [
            {"number": 1, "category": "Missing Schedules",
             "question": "No door schedule was found. How many doors are "
                         "painted, and what type?",
             "action_required": "internal: estimator confirms via manual "
                                "review of the drawings"},
            {"number": 2, "category": "Clarification Needed",
             "question": "Is the exterior CMU in the paint scope?",
             "action_required": "Confirm."},
        ],
        "validation": {"warnings": [
            {"severity": "high", "policy_zero": True,
             "message": "Stair railings were detected but not priced "
                        "(no measurable quantity on the drawings)."},
            {"severity": "low", "message": "minor mismatch"},
        ]},
    }


print("(1) flag semantics")
check("(1a) default OFF", not jobs._delivery_full_package_enabled())
for val, want in (("1", True), ("true", True), ("True", True),
                  ("0", False), ("false", False), ("yes", False),
                  (" 1 ", True)):
    os.environ["NIGHTSHIFT_DELIVERY_FULL_PACKAGE"] = val
    got = jobs._delivery_full_package_enabled()
    check(f"(1b) {val!r} -> {want}", got is want)
os.environ.pop("NIGHTSHIFT_DELIVERY_FULL_PACKAGE", None)


print("(2) flag off = today's behavior, byte-identical")


class _CaptureSMTP:
    sent: list = []

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


def _send(result, **kwargs):
    _CaptureSMTP.sent = []
    jobs.send_result_email({"name": "Anthony", "email": "a@example.com"},
                           result, **kwargs)
    msg = _CaptureSMTP.sent[-1]
    part = msg.get_payload()[0]
    # decode=True: a non-ASCII body (the scope section's punctuation)
    # rides as base64; the flag-off body must compare byte-identical.
    body = part.get_payload(decode=True).decode("utf-8")
    attachments = [p.get_filename() for p in msg.get_payload()[1:]]
    return body, attachments


old_addr, old_pw = jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD
old_smtp = jobs.smtplib.SMTP
jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = "t@x.com", "pw"
jobs.smtplib.SMTP = _CaptureSMTP
try:
    with tempfile.TemporaryDirectory() as wd:
        analysis_pdf = os.path.join(wd, "analysis.pdf")
        estimate_pdf = os.path.join(wd, "org_estimate_3001.pdf")
        takeoff_pdf = os.path.join(wd, "org_takeoff_3001.pdf")
        for p in (analysis_pdf, estimate_pdf, takeoff_pdf):
            with open(p, "wb") as f:
                f.write(b"%PDF-1.4 stub")

        result = _poisoned_result()
        result["output_pdf_path"] = analysis_pdf

        os.environ.pop("NIGHTSHIFT_DELIVERY_FULL_PACKAGE", None)
        body_none, atts_none = _send(result, estimate_pdf_path=estimate_pdf,
                                     submission_id="abc-123")
        body_off, atts_off = _send(result, estimate_pdf_path=estimate_pdf,
                                   takeoff_pdf_path=takeoff_pdf,
                                   submission_id="abc-123")
        check("(2a) body byte-identical with flag off",
              body_off == body_none)
        check("(2b) no takeoff attachment with flag off",
              atts_off == atts_none == ["analysis.pdf",
                                        "org_estimate_3001.pdf"],
              str(atts_off))
        check("(2c) no scope section with flag off",
              "PLEASE CONFIRM SCOPE" not in body_off)

        # Flag ON: takeoff attaches, scope section appears.
        os.environ["NIGHTSHIFT_DELIVERY_FULL_PACKAGE"] = "1"
        body_on, atts_on = _send(result, estimate_pdf_path=estimate_pdf,
                                 takeoff_pdf_path=takeoff_pdf,
                                 submission_id="abc-123")
        check("(2d) takeoff attached with flag on",
              atts_on == ["analysis.pdf", "org_estimate_3001.pdf",
                          "org_takeoff_3001.pdf"], str(atts_on))
        check("(2e) scope section in body with flag on",
              "PLEASE CONFIRM SCOPE" in body_on)
        check("(2f) Steven's framing in body",
              "you decide what goes into the final bid" in body_on)
        check("(2g) takeoff called out in body",
              "Also attached: your complete takeoff" in body_on)

        print("(6) size guard on the takeoff attachment")
        big_takeoff = os.path.join(wd, "org_takeoff_9999.pdf")
        with open(big_takeoff, "wb") as f:
            f.seek(jobs._ATTACH_BUDGET_RAW + MB)
            f.write(b"\0")
        body_big, atts_big = _send(result, estimate_pdf_path=estimate_pdf,
                                   takeoff_pdf_path=big_takeoff,
                                   submission_id="abc-123")
        check("(6a) oversized takeoff omitted, essentials kept",
              atts_big == ["analysis.pdf", "org_estimate_3001.pdf"],
              str(atts_big))
        check("(6b) omission noted in body",
              "too large to attach" in body_big
              and "org_takeoff_9999.pdf" in body_big)
        check("(6c) scope section still present without the attachment",
              "PLEASE CONFIRM SCOPE" in body_big)
        check("(6d) no 'Also attached' claim for an omitted takeoff",
              "Also attached: your complete takeoff" not in body_big)
finally:
    jobs.EMAIL_ADDRESS, jobs.EMAIL_APP_PASSWORD = old_addr, old_pw
    jobs.smtplib.SMTP = old_smtp
    os.environ.pop("NIGHTSHIFT_DELIVERY_FULL_PACKAGE", None)


print("(3) customer takeoff carries no internal voice")
html = TC.build_customer_takeoff_html(_sub(), _org(), _poisoned_result())
low = html.lower()
for marker in ("do not send", "manual review", "manual_review",
               "ready_to_send", "route_to_human", "needs_review",
               "pipeline_flags", "reviewer", "rfi required"):
    check(f"(3a) {marker!r} absent from takeoff", marker not in low)
check("(3b) scrub passes the clean render",
      TC.customer_safety_violations(html, org_name="Profeta Painting") == [])
check("(3c) org branding present", "Profeta Painting" in html)
check("(3d) category quantities render",
      "Paintable walls" in html and "4,210" in html
      and "Stair sections" in html)
check("(3e) per-room table renders",
      "Office 101" in html and "12 x 10 x 9" in html and "396" in html)
check("(3f) unit multiplier shown", "x4" in html)
check("(3g) room notes are NOT rendered",
      "reviewer-only note" not in html)


print("(4) tainted content refuses to render (white-label lesson)")
tainted = _poisoned_result()
tainted["analysis"]["floors"][0]["rooms"][0]["room_name"] = \
    "Lobby — do NOT send this proposal"
try:
    TC.generate_customer_takeoff_pdf(_sub(), _org(), tainted, "/tmp")
    check("(4a) internal-voice room name raises", False)
except ValueError as exc:
    check("(4a) internal-voice room name raises", "do not send" in str(exc))

competitor = _poisoned_result()
competitor["analysis"]["project_info"]["project_name"] = \
    "Rider Painting refit"
viol = TC.customer_safety_violations(
    TC.build_customer_takeoff_html(_sub(), _org("Profeta Painting"),
                                   competitor),
    org_name="Profeta Painting")
check("(4b) competitor name flagged for another org",
      any("Rider" in v for v in viol), str(viol))
viol_own = TC.customer_safety_violations(
    "Rider Painting takeoff", org_name="Rider Painting, Inc.")
check("(4c) an org's own name is not a leak", viol_own == [])
check("(4d) the jobs wrapper treats a scrub refusal as no-takeoff",
      jobs._build_and_upload_customer_takeoff.__doc__ is not None
      and "None" in jobs._build_and_upload_customer_takeoff.__doc__)


print("(5) scope-confirm content")
items = TC.build_scope_confirm_items(_poisoned_result())
kinds = [it["kind"] for it in items]
check("(5a) both funded allowances listed",
      kinds.count("Allowance") == 2, str(kinds))
check("(5b) zero-dollar allowance dropped",
      all("Zeroed" not in it["item"] for it in items))
allow = items[0]
check("(5c) allowance carries item/qty/amount/question",
      "Power Washing" in allow["item"] and allow["qty"] == "12,000"
      and allow["amount"] == 4680.00 and "strike" in allow["question"])
check("(5d) both RFIs listed as questions",
      sum("door schedule" in it["question"] for it in items) == 1
      and sum("exterior CMU" in it["question"] for it in items) == 1)
check("(5e) policy-uncertain line listed at $0",
      any(it["item"] == "Unpriced scope" and it["amount"] == 0.0
          and "Stair railings" in it["question"] for it in items))
check("(5f) low-severity warnings excluded",
      all("minor mismatch" not in it["question"] for it in items))
check("(5g) RFI action_required (estimator dialect) not carried",
      all("estimator confirms" not in str(it) for it in items))

check("(5h) PDF section renders the scope items",
      "Please Confirm Scope" in html
      and "Power Washing" in html
      and "door schedule" in html
      and "$0.00 — not priced" in html)

block = jobs._scope_confirm_email_block(_poisoned_result())
check("(5i) email block has item, qty, amount, question",
      "Power Washing" in block and "Qty: 12,000" in block
      and "$4,680.00" in block and "strike it?" in block)
check("(5j) email block framing",
      "you decide what goes into the final bid" in block)
capped = jobs._scope_confirm_email_block(_poisoned_result(), max_items=2)
check("(5k) long lists cap with a pointer to the PDF",
      "(+3 more" in capped and "takeoff PDF" in capped, capped[-80:])
check("(5l) empty result -> empty block",
      jobs._scope_confirm_email_block({"cost_estimate": {}}) == "")


print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED'} ===")
sys.exit(1 if _fails else 0)
