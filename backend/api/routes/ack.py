"""Acknowledge an alarm from the alert email, without logging in.

Why this is two requests and not one link-click
-----------------------------------------------
A bare ``GET /ack/<token>`` that acknowledges on sight is the obvious design
and it is wrong here. Corporate mail paths follow links before a human ever
sees the message — Outlook Safe Links, Gmail's proxy, spam and DLP scanners.
A GET that mutates would be acked by a scanner seconds after delivery, which
in this system means silently cancelling the remaining escalation rungs: the
exact failure the rungs exist to prevent, arriving as silence.

So GET is read-only and renders a page with a button; the button POSTs. That
is one tap on the phone plus one confirm, still far cheaper than opening the
UI and logging in, and safe against anything that follows links automatically.

The token is the credential: 256 bits of urandom, single-use, expiring, and
scoped to exactly one alarm. It authorises nothing else — not reading other
alarms, not un-acking, not any other alarm's ack.

Self-contained HTML on purpose. This is the page someone opens on a phone at
02:00; it must not depend on the SPA loading, a session existing, or
JavaScript running.
"""
from __future__ import annotations
import html
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

public_router = APIRouter(prefix="/api/alerts/ack")

_CSS = (
    "font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "max-width:34rem;margin:3rem auto;padding:0 1.25rem;color:#1a1a1a;"
)


def _page(title: str, body: str, *, tone: str = "#1E4D2B") -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title></head>"
        f"<body style=\"{_CSS}\">"
        f"<h1 style='font-size:1.15rem;color:{tone};margin:0 0 1rem'>{html.escape(title)}</h1>"
        f"{body}</body></html>"
    )


def _describe(row: dict) -> str:
    sub = f"{row['check_id']} / {row['target']}"
    opened = row.get("opened_at")
    when = opened.strftime("%Y-%m-%d %H:%M UTC") if isinstance(opened, datetime) else "?"
    msg = (row.get("message") or "").split("\n")[0][:160]
    return (
        f"<p style='margin:0 0 .35rem'><strong>{html.escape(sub)}</strong></p>"
        f"<p style='margin:0 0 .35rem;color:#555'>severity "
        f"{html.escape(str(row.get('severity') or '?'))} · opened {html.escape(when)}</p>"
        f"<p style='margin:0 0 1.25rem;color:#555'>{html.escape(msg)}</p>"
    )


@public_router.get("/{token}")
async def ack_form(token: str, request: Request):
    """Read-only. Safe for link prefetchers — acknowledges nothing."""
    store = request.app.state.store
    row = await store.peek_ack_token(token)
    if row is None:
        return _page("Link not recognised",
                     "<p>This acknowledgement link is not valid. It may have been "
                     "mistyped, or the alarm it referred to has been deleted.</p>",
                     tone="#B91C1C")
    if row["used_at"] is not None:
        return _page("Already acknowledged",
                     _describe(row) +
                     "<p>Someone has already acknowledged this alarm, so no further "
                     "reminders will be sent for it.</p>")
    if row["expires_at"] <= datetime.now(timezone.utc):
        return _page("Link expired",
                     _describe(row) +
                     "<p>This link has expired. Acknowledge it from the dashboard "
                     "instead.</p>", tone="#B91C1C")
    if row["closed_at"] is not None:
        return _page("Already resolved",
                     _describe(row) +
                     "<p>This alarm closed on its own, so there is nothing to "
                     "acknowledge and no further reminders will be sent.</p>")

    return _page(
        "Acknowledge this alarm?",
        _describe(row) +
        "<p style='color:#555'>Acknowledging stops the remaining reminder emails "
        "for this alarm. It does not resolve it, and if the condition gets worse "
        "you will be notified again.</p>"
        f"<form method='post' action='{html.escape(str(request.url.path))}'>"
        "<button type='submit' style=\"font:600 15px/1 inherit;padding:.8rem 1.4rem;"
        "background:#1E4D2B;color:#fff;border:0;border-radius:3px;cursor:pointer\">"
        "Acknowledge</button></form>",
    )


@public_router.post("/{token}")
async def ack_apply(token: str, request: Request):
    """Claim the token and acknowledge. Single-use, enforced in SQL."""
    store = request.app.state.store
    row = await store.peek_ack_token(token)
    if row is None:
        return _page("Link not recognised",
                     "<p>This acknowledgement link is not valid.</p>", tone="#B91C1C")

    claim = await store.consume_ack_token(token)
    if claim is None:
        # Lost the race, expired, or already used — all benign, and for the
        # person tapping the button they mean the same thing.
        return _page("Already acknowledged",
                     _describe(row) +
                     "<p>No further reminders will be sent for this alarm.</p>")

    alarm_id = claim["alarm_id"]
    # The token was issued to one address, so the ack carries that person's
    # name rather than "someone". Tokens minted before per-recipient issuance
    # have no address and fall back to the generic actor.
    who = claim.get("recipient") or "email-link"
    await store.ack_alarm(alarm_id, who, "acknowledged from alert email")
    log.info("alarm %s acknowledged via email link by %s", alarm_id, who)
    try:
        await request.app.state.store.pool.execute(
            "INSERT INTO admin_audit (at, user_email, action, target, payload) "
            "VALUES (now(), $1, $2, $3, $4)",
            who, "alarm.ack", f"alarm:{alarm_id}",
            {"via": "email ack link", "recipient": claim.get("recipient")},
        )
    except Exception:
        log.exception("audit write failed for email ack of alarm %s", alarm_id)

    return _page("Acknowledged",
                 _describe(row) +
                 f"<p>Acknowledged as <strong>{html.escape(who)}</strong>. No further "
                 "reminder emails will be sent for this alarm, to anyone. "
                 "If the condition worsens, Sentinel will notify you again.</p>")
