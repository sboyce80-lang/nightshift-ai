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
)

logger = logging.getLogger("nightshift.notifications")

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10


def notifications_configured() -> bool:
    """True iff Resend credentials are set. Safe to call anywhere."""
    return bool(RESEND_API_KEY and RESEND_FROM_EMAIL)


def _send(to_addrs, subject: str, body: str) -> bool:
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
