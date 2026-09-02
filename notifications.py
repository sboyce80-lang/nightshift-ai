#!/usr/bin/env python3
"""
Knight Shift — Account Notification Emails
==========================================
Resend HTTP API wrapper for the sign-up / approval flow:

    notify_admin_of_new_signup(user, org, approve_url)
        Tells every address in ADMIN_EMAILS that a new user has requested
        access. Includes a one-click link to the admin approval dashboard.

    notify_user_of_approval(email, name, org_name, app_url)
        Tells the requesting user their access is approved and they can
        sign in.

    notifications_configured() -> bool
        True iff RESEND_API_KEY and RESEND_FROM_EMAIL are both set. Used
        by /admin/orgs to surface a banner when notifications would
        silently no-op.

Sends fail-loud — if Resend is misconfigured or returns an error we log
at ERROR level (not WARNING) so it shows up in Render logs by default.
We still return False rather than raise, so the caller's request flow
isn't interrupted.
"""

import base64
import logging
import os

import requests

from config import (
    RESEND_API_KEY,
    RESEND_FROM_EMAIL,
    RESEND_FROM_NAME,
    ADMIN_EMAILS,
    PLG_SIGNUP_NOTIFY_EMAILS,
    PLG_SALES_EMAILS,
    PLG_SALES_CC_EMAILS,
    PLG_SALES_CONTACT_EMAIL,
    SUPPORT_CONTACT_EMAIL,
    FOUNDER_CONTACT_EMAIL,
)

logger = logging.getLogger("nightshift.notifications")

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10


def notifications_configured() -> bool:
    """True iff Resend credentials are set. Safe to call anywhere."""
    return bool(RESEND_API_KEY and RESEND_FROM_EMAIL)


# The brand lockup travels WITH the message as an inline CID attachment
# rather than as a remote <img src="https://...">. Remote images depend on
# the reader's client fetching them (Gmail proxies, Outlook blocks by
# default, some clients collapse the element to nothing) — a CID part is
# already in the message and always renders.
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "static", "email_logo.png")
LOGO_CID = "ksai-logo"
_logo_b64_cache = None


def _logo_attachment():
    """Base64 logo as a Resend inline attachment, or None if unreadable."""
    global _logo_b64_cache
    if _logo_b64_cache is None:
        try:
            with open(_LOGO_PATH, "rb") as fh:
                _logo_b64_cache = base64.b64encode(fh.read()).decode("ascii")
        except OSError as exc:
            # Non-fatal: the alt text carries the brand name.
            logger.error("Email logo unreadable at %s: %s", _LOGO_PATH, exc)
            _logo_b64_cache = ""
    if not _logo_b64_cache:
        return None
    return {
        "filename": "knightshiftai.png",
        "content": _logo_b64_cache,
        "content_type": "image/png",
        "content_id": LOGO_CID,
    }


def _send(to_addrs, subject: str, body: str, cc_addrs=None,
          html_body: str = "", attachments=None) -> bool:
    if not RESEND_API_KEY:
        logger.error(
            "Resend not configured (RESEND_API_KEY missing) — "
            "notification dropped: %r → %s", subject, to_addrs)
        return False
    if not RESEND_FROM_EMAIL:
        logger.error(
            "Resend not configured (RESEND_FROM_EMAIL missing) — "
            "notification dropped: %r → %s", subject, to_addrs)
        return False
    if not to_addrs:
        return False

    from_header = f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>"
    payload = {
        "from": from_header,
        "to": list(to_addrs),
        "subject": subject,
        "text": body,
    }
    # Send multipart when an HTML part is supplied. Mail clients that
    # auto-linkify bare URLs in text/plain rewrite them into tracking
    # redirects; real <a> tags render as written.
    if html_body:
        payload["html"] = html_body
    if attachments:
        payload["attachments"] = list(attachments)
    # Don't double-deliver to anyone already on the To: line.
    cc = [a for a in (cc_addrs or []) if a not in set(to_addrs)]
    if cc:
        payload["cc"] = cc

    try:
        resp = requests.post(
            _RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("Resend request failed for %r → %s: %s",
                     subject, to_addrs, exc)
        return False

    if resp.status_code >= 400:
        logger.error("Resend rejected send (%d) for %r → %s: %s",
                     resp.status_code, subject, to_addrs, resp.text[:500])
        return False

    try:
        msg_id = resp.json().get("id", "?")
    except ValueError:
        msg_id = "?"
    logger.info("Notification sent: %r → %s (resend id=%s)",
                subject, to_addrs, msg_id)
    return True


def notify_admin_of_new_signup(user, org, approve_url: str) -> bool:
    """Email all ADMIN_EMAILS about a new access request."""
    if not ADMIN_EMAILS:
        logger.error("No ADMIN_EMAILS configured — skipping new-signup alert "
                     "(would have notified about org=%r)", getattr(org, "name", "?"))
        return False

    body = f"""A new user has requested access to Knight Shift.

  Name:     {user.name or '(not provided)'}
  Email:    {user.email}
  Company:  {org.name}
  Domain:   {org.email_domain or '(personal email)'}

Review and approve at:
  {approve_url}

— Knight Shift
"""
    return _send(
        sorted(ADMIN_EMAILS),
        f"New access request: {org.name}",
        body,
    )


def notify_user_of_approval(email: str, name: str, org_name: str,
                            app_url: str) -> bool:
    """Email the requester that their access is now active."""
    body = f"""Hi {name or 'there'},

Good news — your Knight Shift access for {org_name} has been approved.
You can sign in and start submitting estimates here:

  {app_url}

If you have any questions, just reply to this email.

— Knight Shift
"""
    return _send([email], "Your Knight Shift access is approved", body)


def notify_user_of_org_invite(email: str, org_name: str, role: str,
                              inviter_name: str, inviter_email: str,
                              app_url: str) -> bool:
    """Email an invitee that they've been added to an org.

    Sent from the /account/members/invite handler after the membership row
    commits. The invitee signs in with this email and is auto-joined to the
    org via the existing _sync_user path — no token in the link.
    """
    role_label = "owner" if role == "owner" else "member"
    inviter = inviter_name or inviter_email or "A teammate"
    body = f"""Hi,

{inviter} ({inviter_email}) added you to {org_name} on Knight Shift as a {role_label}.

Sign in with this email address to join automatically:

  {app_url}

If you weren't expecting this, you can ignore this email — no account is
created until you sign in.

— Knight Shift
"""
    return _send([email], f"You've been added to {org_name} on Knight Shift", body)


# ---------------------------------------------------------------------------
# PLG self-serve (freemium) lifecycle emails
# ---------------------------------------------------------------------------

# The details a freemium user fills in so the pricing conversation starts
# with data instead of discovery ping-pong. Shared by the exhausted email
# ("reply with…") and the paywall page's pre-filled mailto body — keep the
# two in sync by editing only this constant. Deliberately short: every
# extra field costs replies.
PRICING_REPLY_TEMPLATE = """Company name:
Approximate annual revenue:
Estimates you run per year:
Estimates you run per week:
Average project size ($):
Number of estimators on staff:
Primary work type (commercial TI / multifamily / healthcare / new build / other):
Best phone number and time to call:"""

def _welcome_html(name, org_name, app_url, allowance, guide_url):
    """HTML part for the welcome email.

    Table-based with inline styles — the lowest common denominator across
    mail clients. Every link is a real anchor with human-readable text, so
    clients that rewrite bare URLs in text/plain have nothing to rewrite.

    Colours are sampled from the KnightShift mark: navy wordmark, blueprint
    blue, plume green. White-on-blue clears 5.8:1.
    """
    navy, body, blue, green, rule = ("#0C1B2D", "#33475B", "#0F67AD",
                                     "#099967", "#DFE5EA")
    # Declared on every text node — mail clients don't inherit reliably, and
    # an unset family renders the whole message in the client's serif default.
    ff = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
          "Arial,sans-serif")
    logo = f"cid:{LOGO_CID}"

    guide_row = f"""
          <p style="font-family:{ff};margin:0 0 6px;font-size:15px;line-height:1.55;color:{body};">
            <strong style="color:{navy};">New to KnightShiftAI?</strong> This one-pager walks
            you through your first bid and what each tab does &mdash; five minutes now
            saves you a re-run later.
          </p>
          <p style="font-family:{ff};margin:0 0 28px;font-size:15px;line-height:1.55;">
            <a href="{guide_url}" style="color:{blue};font-weight:600;">Read the getting-started guide &rarr;</a>
          </p>""" if guide_url else ""

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#EEF1F4;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       bgcolor="#EEF1F4" style="background-color:#EEF1F4;">
  <tr><td align="center" style="padding:26px 12px 34px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
           bgcolor="#FFFFFF"
           style="width:600px;max-width:100%;background-color:#FFFFFF;border:1px solid {rule};border-radius:4px;">

      <!-- Brand lockup. alt carries the name when images are blocked. -->
      <tr><td align="center" style="padding:30px 34px 0;">
        <img src="{logo}" width="220" alt="KnightShiftAI &mdash; Forged by Willpower"
             style="width:220px;max-width:70%;height:auto;display:block;border:0;outline:none;">
      </td></tr>
      <tr><td style="padding:22px 34px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td height="3" bgcolor="{blue}" style="background-color:{blue};height:3px;line-height:3px;font-size:0;width:64px;">&nbsp;</td>
            <td height="3" bgcolor="{green}" style="background-color:{green};height:3px;line-height:3px;font-size:0;width:26px;">&nbsp;</td>
            <td height="3" bgcolor="{rule}" style="background-color:{rule};height:3px;line-height:3px;font-size:0;">&nbsp;</td>
          </tr>
        </table>
      </td></tr>

      <tr><td style="padding:26px 34px 6px;">
        <h1 style="font-family:{ff};margin:0 0 18px;font-size:22px;line-height:1.25;color:{navy};font-weight:700;">
          Hi {name or 'there'}, your account is ready
        </h1>
        <p style="font-family:{ff};margin:0 0 14px;font-size:15px;line-height:1.55;color:{body};">
          Welcome to KnightShiftAI &mdash; your account for
          <strong style="color:{navy};">{org_name}</strong> is ready to go.
        </p>
        <p style="font-family:{ff};margin:0 0 24px;font-size:15px;line-height:1.55;color:{body};">
          {allowance} Upload a bid set (plans and finish schedules as PDFs)
          and we'll email you a full takeoff and estimate, usually the same day.
        </p>

        <!-- Button: colour is set on the td (attribute + property) AND on the
             anchor itself, so a client that drops any one of the three still
             renders readable text rather than white-on-white. -->
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 28px;">
          <tr><td bgcolor="{blue}" align="center"
                  style="background-color:{blue};border-radius:4px;">
            <a href="{app_url}" style="font-family:{ff};display:inline-block;
               background-color:{blue};border:1px solid {blue};border-radius:4px;
               padding:13px 28px;font-size:15px;font-weight:600;color:#FFFFFF;
               text-decoration:none;">Upload your first bid</a>
          </td></tr>
        </table>
{guide_row}
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="border-top:1px solid {rule};margin:0 0 20px;"><tr><td style="height:20px;line-height:20px;font-size:0;">&nbsp;</td></tr></table>

        <p style="font-family:{ff};margin:0 0 10px;font-size:11px;letter-spacing:.13em;text-transform:uppercase;color:{green};font-weight:700;">Tips for the best results</p>
        <p style="font-family:{ff};margin:0 0 6px;font-size:15px;line-height:1.55;color:{body};">
          Include the finish schedules and floor plans, not just a cover sheet.
        </p>
        <p style="font-family:{ff};margin:0 0 26px;font-size:15px;line-height:1.55;color:{body};">
          One project per submission.
        </p>

        <p style="font-family:{ff};margin:0 0 8px;font-size:15px;line-height:1.55;color:{body};">
          Questions at any point? Just reply to this email &mdash; a real person reads it.
          You can also reach us directly:
        </p>
        <p style="font-family:{ff};margin:0 0 30px;font-size:15px;line-height:1.7;color:{body};">
          General support &middot;
          <a href="mailto:{SUPPORT_CONTACT_EMAIL}" style="color:{blue};">{SUPPORT_CONTACT_EMAIL}</a><br>
          Steve, Co-founder and Head of Technology &middot;
          <a href="mailto:{FOUNDER_CONTACT_EMAIL}" style="color:{blue};">{FOUNDER_CONTACT_EMAIL}</a>
        </p>
      </td></tr>

      <tr><td bgcolor="{navy}" style="background-color:{navy};padding:16px 34px;border-radius:0 0 4px 4px;">
        <p style="font-family:{ff};margin:0;font-size:12px;letter-spacing:.11em;text-transform:uppercase;color:#FFFFFF;font-weight:600;">KnightShiftAI</p>
        <p style="font-family:{ff};margin:3px 0 0;font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:{green};">Forged by Willpower</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def notify_welcome(email: str, name: str, org_name: str, app_url: str,
                   bid_limit=None, guide_url: str = "") -> bool:
    """Welcome an approved user — the one email every new account gets.

    Two callers, one voice:
      - self-serve signup auto-approved onto freemium (bid_limit=5)
      - an admin hand-approving a waitlisted org, which lands on plan='beta'
        with no quota (bid_limit=None → the unlimited wording)

    Sent multipart: the HTML part carries labelled links, the text part is
    the plain-reader fallback.
    """
    if bid_limit is None:
        allowance = ("Your account has no bid limit — upload as many projects "
                     "as you like.")
        allowance_html = ("Your account has no bid limit &mdash; upload as many "
                          "projects as you like.")
        subject = f"Welcome to KnightShiftAI — {org_name} is approved"
    else:
        allowance = (f"You have {bid_limit} free bids to try the system on "
                     f"your own projects.")
        allowance_html = (f"You have <strong>{bid_limit} free bids</strong> to try "
                          f"the system on your own projects.")
        subject = f"Welcome to KnightShiftAI — {bid_limit} free bids inside"

    guide_block = f"""

New to KnightShiftAI? This one-pager walks you through your first bid and
what each tab does — five minutes now saves you a re-run later:

  {guide_url}""" if guide_url else ""

    body = f"""Hi {name or 'there'},

Welcome to KnightShiftAI — your account for {org_name} is ready to go.

{allowance}
Upload a bid set (plans + finish schedules as PDFs) and we'll email you a
full takeoff and estimate, usually the same day:

  {app_url}{guide_block}

Tips for the best results:
  - Include the finish schedules and floor plans, not just a cover sheet.
  - One project per submission.

Questions at any point? Just reply to this email — a real person reads it.
You can also reach us directly:

  - General support: {SUPPORT_CONTACT_EMAIL}
  - Steve, Co-founder and Head of Technology: {FOUNDER_CONTACT_EMAIL}

— KnightShiftAI
"""
    logo = _logo_attachment()
    return _send([email], subject, body,
                 html_body=_welcome_html(name, org_name, app_url,
                                         allowance_html, guide_url),
                 attachments=[logo] if logo else None)


# Back-compat alias — the freemium signup path has always called this name.
notify_freemium_welcome = notify_welcome


def notify_internal_plg_signup(user_email: str, user_name: str, user_title: str,
                               org_name: str, org_domain, phone: str,
                               company_size: str, auto_approved: bool) -> bool:
    """Tell the team a self-serve signup just landed (auto-approved or not)."""
    to = PLG_SIGNUP_NOTIFY_EMAILS or ADMIN_EMAILS
    if not to:
        logger.error("No PLG_SIGNUP_NOTIFY_EMAILS/ADMIN_EMAILS configured — "
                     "dropping PLG signup alert for %r", org_name)
        return False

    status = ("auto-approved on the freemium plan" if auto_approved
              else "NOT auto-approved (free-email signup) — on the manual waitlist")
    body = f"""New self-serve signup ({status}).

  Name:          {user_name or '(not provided)'}
  Title:         {user_title or '(not provided)'}
  Email:         {user_email}
  Phone:         {phone or '(not provided)'}
  Company:       {org_name}
  Company size:  {company_size or '(not provided)'}
  Domain:        {org_domain or '(personal email)'}

— Knight Shift
"""
    return _send(sorted(to), f"PLG signup: {org_name}", body)


def notify_freemium_exhausted(email: str, name: str, org_name: str,
                              bid_limit: int) -> bool:
    """Tell a freemium user they've used their last free bid — active CTA."""
    body = f"""Hi {name or 'there'},

You've just run the last of your {bid_limit} free bids on Knight Shift —
thanks for putting the system through its paces.

To keep bidding with unlimited takeoffs, let's find the right plan for
{org_name}. Reply to this email (or write us at {PLG_SALES_CONTACT_EMAIL})
with the details below and we'll come back with pricing the same day:

{PRICING_REPLY_TEMPLATE}

Your estimates stay available in your account either way.

— Knight Shift
"""
    return _send([email],
                 f"You've used your {bid_limit} free bids — let's talk pricing",
                 body)


def notify_internal_freemium_milestone(org_name: str, owner_emails,
                                       bids_used: int, bid_limit: int,
                                       exhausted: bool) -> bool:
    """Alert sales that a freemium org is heavily using (or out of) bids.

    Fired at FREEMIUM_HOT_LEAD_THRESHOLD (call them while they're still
    bidding) and again at exhaustion.
    """
    to = PLG_SALES_EMAILS or ADMIN_EMAILS
    if not to:
        logger.error("No PLG_SALES_EMAILS/ADMIN_EMAILS configured — dropping "
                     "freemium milestone alert for %r", org_name)
        return False

    stage = ("EXHAUSTED — paywall is now up" if exhausted
             else f"hot lead — {bids_used} of {bid_limit} free bids used")
    owners = ", ".join(sorted(owner_emails)) or "(unknown)"
    body = f"""Freemium usage alert: {org_name} — {stage}.

  Bids used:  {bids_used} / {bid_limit}
  Owner(s):   {owners}

{'They just hit the paywall; the exhausted email with our contact address went out. Follow up today.' if exhausted else 'They are actively bidding right now — a call today beats an email after they hit the wall.'}

— Knight Shift
"""
    subject = (f"Freemium exhausted: {org_name}" if exhausted
               else f"Hot lead: {org_name} ({bids_used}/{bid_limit} bids)")
    return _send(sorted(to), subject, body, cc_addrs=sorted(PLG_SALES_CC_EMAILS))


def notify_user_of_denial(email: str, name: str, org_name: str) -> bool:
    """Email the requester that their access request was denied."""
    body = f"""Hi {name or 'there'},

Thank you for your interest in Knight Shift. After reviewing your access
request for {org_name}, we're unable to approve it at this time.

If you believe this was a mistake or would like to discuss further, please
reply to this email.

— Knight Shift
"""
    return _send([email], "Your Knight Shift access request", body)
