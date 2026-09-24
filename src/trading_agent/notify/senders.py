"""Email + SMS delivery over Resend's HTTPS API (plan §12 notification channel).

Not SMTP: this project runs in a cloud sandbox whose network only proxies
HTTPS egress — a raw SMTP socket (port 587/465) times out at connect(), a
network-level block confirmed directly, not a code bug. Resend's REST API
sends over HTTPS like every other integration in this project (Perplexity,
Alpaca), so it actually works from an unattended cloud routine. SMS rides the
same API to a phone's carrier email-to-SMS gateway address rather than a
separate SMS provider.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from trading_agent.config import require_env

RESEND_URL = "https://api.resend.com/emails"
# Resend's no-setup sandbox sender — works without verifying a domain, so
# there's a working default even before RESEND_FROM_ADDRESS is configured.
DEFAULT_FROM_ADDRESS = "onboarding@resend.dev"


def _send_via_resend(to_addr: str, subject: str, text: str, html: str | None = None) -> None:
    api_key = require_env("RESEND_API_KEY")
    from_addr = os.environ.get("RESEND_FROM_ADDRESS", DEFAULT_FROM_ADDRESS)

    payload: dict[str, Any] = {"from": from_addr, "to": [to_addr], "subject": subject, "text": text}
    if html:
        payload["html"] = html

    response = requests.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    response.raise_for_status()


def send_email(subject: str, body: str, html_body: str | None = None) -> bool:
    """Send to NOTIFY_EMAIL_ADDRESS. Returns False (no error) when that's
    unset — no destination configured is a normal, silent no-op, same as an
    unset SMS_GATEWAY_ADDRESS. A configured destination with a broken/missing
    RESEND_API_KEY still raises; that's a real config error, not a missing
    optional channel.
    """
    to_addr = os.environ.get("NOTIFY_EMAIL_ADDRESS")
    if not to_addr:
        return False
    _send_via_resend(to_addr, subject, body, html_body)
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
    _send_via_resend(to_addr, subject="Bull-Trading", text=body)
    return True
