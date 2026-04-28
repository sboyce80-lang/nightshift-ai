#!/usr/bin/env python3
"""
Knight Shift — Account Notification Emails
==========================================
SMTP helpers for the sign-up / approval flow:

    notify_admin_of_new_signup(user, org, approve_url)
        Tells every address in ADMIN_EMAILS that a new user has requested
        access. Includes a one-click link to the admin approval dashboard.

    notify_user_of_approval(email, name, org_name, app_url)
        Tells the requesting user their access is approved and they can
        sign in.

Both fail soft — if SMTP credentials aren't set or sending raises, we log
and return rather than blocking the request that triggered the email.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    ADMIN_EMAILS,
)

logger = logging.getLogger("nightshift.notifications")

_FROM_NAME = "Knight Shift"


def _send(to_addrs, subject: str, body: str) -> bool:
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — skipping notification (subject=%r)",
                       subject)
        return False
    if not to_addrs:
        return False

    msg = MIMEMultipart()
    msg["From"] = f"{_FROM_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Notification sent: %r → %s", subject, to_addrs)
        return True
    except Exception as exc:
        logger.error("Failed to send notification %r: %s", subject, exc)
        return False


def notify_admin_of_new_signup(user, org, approve_url: str) -> bool:
    """Email all ADMIN_EMAILS about a new access request."""
    if not ADMIN_EMAILS:
        logger.warning("No ADMIN_EMAILS configured — skipping new-signup alert")
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
