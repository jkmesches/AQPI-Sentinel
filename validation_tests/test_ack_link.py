#!/usr/bin/env python3
"""Acking from the alert email must be easy for a person and impossible for a
link scanner.

The escalation ladder (0m / 40m / 6h) is the answer to "did anyone see this",
and an ack is what stops it. That only works if acking is cheaper than
ignoring the mail, hence the link. But the same property that makes the link
convenient makes it dangerous: Outlook Safe Links, Gmail's proxy, and DLP
scanners all follow URLs in mail before a human reads it. A GET that
acknowledges on sight would be claimed by a scanner seconds after delivery
and would cancel the remaining rungs — arriving, from the operator's side, as
silence. That is precisely the failure the rungs exist to prevent.

So GET is read-only and POST mutates. These assertions pin that, plus the
token's own guarantees: single-use, expiring, one alarm.

Run:  python3 validation_tests/test_ack_link.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend.api.routes import ack as ack_routes          # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


NOW = datetime.now(timezone.utc)


def row(**over):
    base = {"alarm_id": 7, "used_at": None, "expires_at": NOW + timedelta(days=7),
            "recipient": "eunyeol.kim@colostate.edu",
            "check_id": "layer2.radar.XSWR", "target": "XSWR", "severity": "critical",
            "opened_at": NOW - timedelta(hours=2), "closed_at": None,
            "message": "declared=DOWN obs=0 → CONFIRMED_DOWN"}
    base.update(over)
    return base


class StubStore:
    def __init__(self, r):
        self.r = r
        self.consumed: list[str] = []
        self.acked: list[tuple] = []
        self.token_live = True
        self.pool = self

    async def peek_ack_token(self, token):
        return self.r

    async def consume_ack_token(self, token):
        self.consumed.append(token)
        if not self.token_live:
            return None
        self.token_live = False          # single-use
        return {"alarm_id": self.r["alarm_id"], "recipient": self.r.get("recipient")}

    async def ack_alarm(self, alarm_id, user, note):
        self.acked.append((alarm_id, user, note))

    async def execute(self, *a, **k):    # audit insert
        return None


class Req:
    def __init__(self, store):
        self.app = type("A", (), {"state": type("S", (), {"store": store})()})()
        self.url = type("U", (), {"path": "/api/alerts/ack/tok"})()


async def main() -> int:
    print("the scanner-safety property:")
    st = StubStore(row())
    resp = await ack_routes.ack_form("tok", Req(st))
    body = resp.body.decode()
    check("GET acknowledges nothing", st.consumed == [] and st.acked == [],
          f"consumed={st.consumed} acked={st.acked}")
    check("...and says so by offering a button instead", "<form method='post'" in body)
    check("...the button posts rather than links", "method='post'" in body
          and "<a href" not in body.split("<form")[1])

    print("\nthe person's path:")
    st = StubStore(row())
    resp = await ack_routes.ack_apply("tok", Req(st))
    check("POST claims the token exactly once", st.consumed == ["tok"], str(st.consumed))
    check("...and acknowledges the alarm it was issued for",
          st.acked and st.acked[0][0] == 7, str(st.acked))
    check("...attributed to the person the token was issued to",
          st.acked and st.acked[0][1] == "eunyeol.kim@colostate.edu", str(st.acked))
    check("...and the page names them back",
          "eunyeol.kim@colostate.edu" in resp.body.decode())

    # Tokens minted before per-recipient issuance carry no address. They must
    # still work — just without a name on them.
    st = StubStore(row(recipient=None))
    await ack_routes.ack_apply("tok", Req(st))
    check("a token with no recipient still acks, unattributed",
          st.acked and st.acked[0][1] == "email-link", str(st.acked))
    check("...and confirms it", "Acknowledged" in resp.body.decode())

    print("\nthe token's guarantees:")
    st = StubStore(row())
    await ack_routes.ack_apply("tok", Req(st))
    st.acked.clear()
    await ack_routes.ack_apply("tok", Req(st))       # second tap
    check("a token cannot be spent twice", st.acked == [], str(st.acked))

    st = StubStore(row(used_at=NOW - timedelta(minutes=5)))
    resp = await ack_routes.ack_form("tok", Req(st))
    check("an already-used token offers no button",
          "<form" not in resp.body.decode() and "Already acknowledged" in resp.body.decode())

    st = StubStore(row(expires_at=NOW - timedelta(seconds=1)))
    resp = await ack_routes.ack_form("tok", Req(st))
    check("an expired token offers no button",
          "<form" not in resp.body.decode() and "expired" in resp.body.decode().lower())

    st = StubStore(row(closed_at=NOW - timedelta(minutes=1)))
    resp = await ack_routes.ack_form("tok", Req(st))
    check("an alarm that already closed offers no button",
          "<form" not in resp.body.decode(), resp.body.decode()[:80])

    st = StubStore(None)
    resp = await ack_routes.ack_form("nope", Req(st))
    check("an unknown token reveals nothing and offers no button",
          "<form" not in resp.body.decode() and "layer2" not in resp.body.decode())

    print("\nwhat the page tells the reader:")
    st = StubStore(row())
    body = (await ack_routes.ack_form("tok", Req(st))).body.decode()
    check("it names the alarm being acknowledged", "XSWR" in body)
    check("...and is explicit that acking is not resolving",
          "does not resolve" in body)
    check("...and that a worsening condition will still reach them",
          "worse" in body or "worsens" in body)

    print("\nper-recipient links (why the sink sends one message each):")
    from backend.config import SETTINGS
    from backend.alarms.sinks import email as email_sink
    alarm = {"id": 7, "check_id": "layer2.radar.XSWR", "target": "XSWR",
             "stage": "L2", "severity": "critical", "message": "down",
             "opened_at": NOW, "payload": {},
             "_ack_tokens": {"a@x.test": "TOKEN_A", "b@x.test": "TOKEN_B"}}
    route = type("R", (), {"policy": "page-team"})()
    object.__setattr__(SETTINGS, "public_url", "https://s.example") \
        if hasattr(SETTINGS, "__dataclass_fields__") else None
    ctx_a = email_sink._build_ctx(alarm, route, 0, recipient="a@x.test")
    ctx_b = email_sink._build_ctx(alarm, route, 0, recipient="b@x.test")
    check("each recipient gets a DIFFERENT ack link",
          ctx_a["ack_url"] != ctx_b["ack_url"], f"{ctx_a['ack_url']} vs {ctx_b['ack_url']}")
    check("...carrying that recipient's own token",
          "TOKEN_A" in (ctx_a["ack_url"] or "") and "TOKEN_B" in (ctx_b["ack_url"] or ""))
    check("...and an address with no token gets no link",
          email_sink._build_ctx(alarm, route, 0, recipient="c@x.test")["ack_url"] is None)
    check("...as does a render with no recipient at all",
          email_sink._build_ctx(alarm, route, 0)["ack_url"] is None)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall ack-link assertions passed")
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
