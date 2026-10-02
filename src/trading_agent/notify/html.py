"""Inline-styled HTML building blocks shared by every notification email.

Per user instruction 2026-09-30 ("crisp and mobile friendly" per-checkpoint
digests, a "richer font n colors" daily summary): plain functions, not a
templating engine — email clients (especially mobile Gmail/Apple Mail) are
far more consistent with plain inline styles than with <style> blocks or
external CSS, so every function here returns a fully self-contained HTML
string with no external dependency (no webfonts, no linked stylesheet).
send_email() already accepts an optional html_body alongside the required
plain-text body — the plain text stays the source of truth for clients that
strip HTML, this is purely the richer rendering for ones that don't.
"""

from __future__ import annotations

FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

COLORS = {
    "buy": "#15803d",
    "buy_bg": "#dcfce7",
    "sell": "#b91c1c",
    "sell_bg": "#fee2e2",
    "text": "#0f172a",
    "muted": "#64748b",
    "border": "#e2e8f0",
    "alert": "#b45309",
    "alert_bg": "#fef3c7",
    "positive": "#15803d",
    "negative": "#b91c1c",
    "header_bg": "#0f172a",
    "header_text": "#ffffff",
    "card_bg": "#f8fafc",
    "chip_bg": "#f1f5f9",
}


def wrap(title: str, subtitle: str, inner_html: str) -> str:
    """The outer shell every notification email shares: a mobile-width
    single column (max 600px, but fluid below that — no fixed-width
    horizontal scroll on a phone), the system font stack (renders natively
    per-platform, no webfont download needed), and generous padding for a
    phone screen rather than a desktop-dense layout.
    """
    subtitle_html = (
        f'<div style="font-size:13px;color:#cbd5e1;margin-top:2px;">{subtitle}</div>' if subtitle else ""
    )
    return (
        f'<div style="font-family:{FONT_STACK};max-width:600px;width:100%;margin:0 auto;'
        f'background-color:#ffffff;color:{COLORS["text"]};">'
        f'<div style="background-color:{COLORS["header_bg"]};color:{COLORS["header_text"]};'
        f'padding:18px 20px;">'
        f'<div style="font-size:19px;font-weight:700;">{title}</div>'
        f"{subtitle_html}"
        f"</div>"
        f'<div style="padding:18px 20px;">{inner_html}</div>'
        f"</div>"
    )


def badge(action: str) -> str:
    action = action.upper()
    if action == "BUY":
        bg, fg = COLORS["buy_bg"], COLORS["buy"]
    elif action == "SELL":
        bg, fg = COLORS["sell_bg"], COLORS["sell"]
    else:
        bg, fg = COLORS["chip_bg"], COLORS["muted"]
    return (
        f'<span style="display:inline-block;background-color:{bg};color:{fg};'
        f'font-weight:700;font-size:12px;letter-spacing:0.03em;padding:3px 10px;'
        f'border-radius:12px;vertical-align:middle;">{action}</span>'
    )


def signed_pct(value: float | None, decimals: int = 2) -> str:
    """A +/- percentage, colored green (>=0) or red (<0) — for return figures."""
    if value is None:
        return f'<span style="color:{COLORS["muted"]};">unavailable</span>'
    color = COLORS["positive"] if value >= 0 else COLORS["negative"]
    sign = "+" if value >= 0 else ""
    return f'<span style="color:{color};font-weight:700;">{sign}{value:.{decimals}f}%</span>'


def signed_dollar(value: float | None, decimals: int = 2) -> str:
    """A +/- dollar amount, colored green (>=0) or red (<0) — same convention as signed_pct()."""
    if value is None:
        return f'<span style="color:{COLORS["muted"]};">unavailable</span>'
    color = COLORS["positive"] if value >= 0 else COLORS["negative"]
    sign = "+" if value >= 0 else "-"
    return f'<span style="color:{color};font-weight:700;">{sign}${abs(value):,.{decimals}f}</span>'


def alert_banner(text: str) -> str:
    return (
        f'<div style="background-color:{COLORS["alert_bg"]};color:{COLORS["alert"]};'
        f'border-left:4px solid {COLORS["alert"]};padding:10px 14px;margin-bottom:16px;'
        f'font-weight:600;font-size:14px;border-radius:4px;">⚠️ {text}</div>'
    )


def section_heading(text: str) -> str:
    return (
        f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.06em;color:{COLORS["muted"]};margin:20px 0 8px;">{text}</div>'
    )


def rec_row(ticker: str, action: str, confidence: float, rationale: str, extra: str = "") -> str:
    """One actionable recommendation — ticker, action badge, confidence,
    and its one-line rationale. `extra` appends more detail (e.g. suggested
    size) to the confidence line without cluttering the main row.
    """
    rationale_html = f'<div style="font-size:14px;margin-top:4px;line-height:1.4;">{rationale}</div>' if rationale else ""
    return (
        f'<div style="border-bottom:1px solid {COLORS["border"]};padding:12px 0;">'
        f'<span style="font-size:17px;font-weight:700;">{ticker}</span>'
        f'<span style="margin-left:8px;">{badge(action)}</span>'
        f'<div style="font-size:13px;color:{COLORS["muted"]};margin-top:3px;">'
        f"Confidence {confidence:g}%{extra}</div>"
        f"{rationale_html}"
        f"</div>"
    )


def stat_card(label: str, value_html: str) -> str:
    return (
        f'<div style="background-color:{COLORS["card_bg"]};border-radius:8px;'
        f'padding:12px 16px;margin-bottom:10px;">'
        f'<div style="font-size:12px;color:{COLORS["muted"]};font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.04em;">{label}</div>'
        f'<div style="font-size:22px;margin-top:2px;">{value_html}</div>'
        f"</div>"
    )


def chip(text: str, kind: str = "neutral") -> str:
    """A small inline pill for a status word (submitted/refused/skipped/
    error, approved/rejected, auto/human)."""
    palette = {
        "positive": (COLORS["buy_bg"], COLORS["buy"]),
        "negative": (COLORS["sell_bg"], COLORS["sell"]),
        "neutral": (COLORS["chip_bg"], COLORS["muted"]),
    }
    bg, fg = palette.get(kind, palette["neutral"])
    return (
        f'<span style="display:inline-block;background-color:{bg};color:{fg};'
        f'font-weight:600;font-size:12px;padding:2px 9px;border-radius:10px;">{text}</span>'
    )


def muted(text: str) -> str:
    return f'<div style="font-size:14px;color:{COLORS["muted"]};padding:8px 0;">{text}</div>'
