"""Email + SMS delivery (plan §12 notification channel).

SMTP-only, on purpose: it's the one transport that fits the credential
policy without adding a provider-specific SDK or OAuth flow. SMS rides the
same SMTP send to a phone's carrier email-to-SMS gateway address
(SMS_GATEWAY_ADDRESS, e.g. `<number>@tmomail.net`) rather than a separate
provider. Every address and credential comes from the environment — see
CLAUDE.md's credential policy; nothing here ever reads or writes a file.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from trading_agent.config import require_env


def _send_via_smtp(to_addr: str, subject: str, body: str, html_body: str | None = None) -> None:
    host = require_env("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = require_env("SMTP_USERNAME")
    password = require_env("SMTP_PASSWORD")

    if html_body:
        msg: MIMEMultipart | MIMEText = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr

    with smtplib.SMTP(host, port, timeout=10) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(username, [to_addr], msg.as_string())


def send_email(subject: str, body: str, html_body: str | None = None) -> bool:
    """Send to NOTIFY_EMAIL_ADDRESS. Returns False (no error) when that's
    unset — no destination configured is a normal, silent no-op, same as an
    unset SMS_GATEWAY_ADDRESS. A configured destination with broken SMTP
    credentials still raises; that's a real config error, not a missing
    optional channel.
    """
    to_addr = os.environ.get("NOTIFY_EMAIL_ADDRESS")
    if not to_addr:
        return False
    _send_via_smtp(to_addr, subject, body, html_body)
    return True


def send_sms(body: str) -> bool:
    """Send a short plain-text message to SMS_GATEWAY_ADDRESS. Returns False
    (no error) when that's unset. Carrier gateways vary in how much they'll
    carry — keep `body` short; callers should not rely on a subject line
    surviving the gateway.
    """
    to_addr = os.environ.get("SMS_GATEWAY_ADDRESS")
    if not to_addr:
        return False
    _send_via_smtp(to_addr, subject="", body=body)
    return True
