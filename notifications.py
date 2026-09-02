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

import logging

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


def _send(to_addrs, subject: str, body: str, cc_addrs=None) -> bool:
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

def notify_welcome(email: str, name: str, org_name: str, app_url: str,
                   bid_limit=None, guide_url: str = "") -> bool:
    """Welcome an approved user — the one email every new account gets.

    Two callers, one voice:
      - self-serve signup auto-approved onto freemium (bid_limit=5)
      - an admin hand-approving a waitlisted org, which lands on plan='beta'
        with no quota (bid_limit=None → the unlimited wording)
    """
    if bid_limit is None:
        allowance = ("Your account has no bid limit — upload as many projects "
                     "as you like.")
        subject = f"Welcome to KnightShiftAI — {org_name} is approved"
    else:
        allowance = (f"You have {bid_limit} free bids to try the system on "
                     f"your own projects.")
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
    return _send([email], subject, body)


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
