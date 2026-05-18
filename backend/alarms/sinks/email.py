"""Email sink — aiosmtplib + Jinja2 templates (plain + HTML alternative).

The default template lives at ``backend/alarms/templates/default.{txt,html}``.
``EmailSink.send`` builds a per-check context (observed values, thresholds,
verification URLs, dashboard link, captured-image link for L4) so the
operator can read the email and either act immediately or open the
linked URLs to verify. Both bodies are attached as
``multipart/alternative`` so clients pick whichever they prefer.

If SMTP isn't configured the sink raises in __init__, which the loader
treats as "channel unavailable, log and skip."
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...config import SETTINGS
from .base import SinkResult

log = logging.getLogger(__name__)

_TPL_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TPL_DIR)),
    autoescape=select_autoescape(default_for_string=False,
                                 enabled_extensions=("html",),
                                 disabled_extensions=("txt",)),
)


# Where the check happened — used to synthesize "verify yourself" links
# pointing at the upstream API endpoints. Same value the checks scrape.
_UPSTREAM = SETTINGS.base.rstrip("/")


from ...stages import stage_descriptor as _stage_descriptor, stage_tech as _stage_tech


def _fmt_age(seconds: float) -> str:
    """Compact relative age: 47s · 12m · 1h23m · 6d12h."""
    s = int(seconds)
    if s < 0:
        return f"+{_fmt_age(-s)} (future)"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec}s" if sec else f"{m}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h}h{m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d{h}h" if h else f"{d}d"


def _opened_age(opened_at: Any) -> str:
    """Wall-clock seconds since the alarm opened, formatted compactly."""
    if isinstance(opened_at, datetime):
        dt = opened_at
    elif isinstance(opened_at, str):
        try:
            dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _fmt_age((datetime.now(timezone.utc) - dt).total_seconds())


def _opened_str(opened_at: Any) -> str:
    if isinstance(opened_at, datetime):
        return opened_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(opened_at, str):
        return opened_at[:19].replace("T", " ") + " UTC"
    return str(opened_at)


def _build_ctx(alarm: dict, route: Any, step_idx: int) -> dict:
    """Build the per-check rendering context.

    Mirrors the timeline drill-down's verifyContext: thresholds vs
    observed values + clickable upstream URLs + dashboard link. Per-
    check-family logic — see explainRun in frontend timeline page for
    the matching structure on the UI side.
    """
    check_id: str = alarm.get("check_id", "")
    target: str = alarm.get("target", "") or ""
    severity: str = (alarm.get("severity") or "warn").lower()
    stage: str = alarm.get("stage", "")

    # Alarm payload nests the original check result under result_payload.
    apl = alarm.get("payload") or {}
    rpl = apl.get("result_payload") if isinstance(apl, dict) else {}
    rpl = rpl if isinstance(rpl, dict) else {}
    status_at_open = (apl.get("status_at_open") if isinstance(apl, dict) else "") or ""

    observed: list[tuple[str, str]] = []
    verify_urls: list[tuple[str, str]] = []
    captured_image_url: str | None = None
    what: str | None = None     # one-paragraph plain-English explanation

    # -------- L0 ----------------------------------------------------
    if check_id == "layer0.website.public":
        verify_urls.append(("public page", f"{_UPSTREAM}/public"))
        if "http" in rpl:  observed.append(("HTTP status", str(rpl["http"])))
        if "bytes" in rpl: observed.append(("body size", f"{rpl['bytes']:,} B"))
        what = "The main public dashboard page didn't return a valid populated response."
    elif check_id == "layer0.website.root_notfound":
        verify_urls.append(("root URL", f"{_UPSTREAM}/"))
        if "http" in rpl: observed.append(("HTTP status", str(rpl["http"])))
        what = "The bare-root URL stopped returning the expected not-found body."
    elif check_id == "layer0.origin.alive":
        verify_urls.append(("origin /api/radar-status/", f"{_UPSTREAM}/api/radar-status/"))
        if "http" in rpl:        observed.append(("HTTP status", str(rpl["http"])))
        if "elapsed_ms" in rpl:  observed.append(("latency", f"{rpl['elapsed_ms']} ms"))
        what = "The origin server didn't respond cleanly to a no-cache probe."
    elif check_id == "layer0.tls.cert":
        if "expires_in_days" in rpl:
            observed.append(("days until expiry", str(rpl["expires_in_days"])))
        what = "TLS certificate is nearing expiry (or invalid)."

    # -------- L1 product --------------------------------------------
    elif check_id.startswith("layer1.product."):
        product = target or check_id.split(".")[-1]
        details = (rpl.get("details_file") or
                   f"{product}/details.json")
        verify_urls.append(("productDetail",
                            f"{_UPSTREAM}/api/productDetail?file={quote(details, safe='/')}"))
        img_src = rpl.get("image_source")
        if img_src:
            verify_urls.append(("latest scan PNG",
                                f"{_UPSTREAM}/api/imageData?file={quote(img_src, safe='/')}"))
        if rpl.get("last_ts"):
            try:
                last = datetime.fromisoformat(rpl["last_ts"].replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - last).total_seconds()
                observed.append(("newest scan age", _fmt_age(age)))
            except ValueError:
                observed.append(("newest scan ts", rpl["last_ts"]))
        if "n_steps" in rpl:      observed.append(("step count", str(rpl["n_steps"])))
        if "image_bytes" in rpl:  observed.append(("image size", f"{rpl['image_bytes']:,} B"))
        if "image_http" in rpl:   observed.append(("image HTTP", str(rpl["image_http"])))
        sub = rpl.get("sub_status") or {}
        failing = [f"{k}={v}" for k, v in sub.items() if v not in ("pass", "skip")]
        if failing:
            observed.append(("failing sub-checks", ", ".join(failing)))
            what = f"Product check tripped sub-verdicts: {', '.join(failing)}."

    # -------- L2 radar reconciliation -------------------------------
    elif check_id.startswith("layer2.radar."):
        folder = rpl.get("folder", "")
        verify_urls.append(("radar-status",
                            f"{_UPSTREAM}/api/radar-status/"))
        if folder:
            verify_urls.append(("xbandRadarImages (Reflectivity)",
                                f"{_UPSTREAM}/api/xbandRadarImages/"
                                f"?radarFolder={folder}&productPrefix=CorrReflectivity"))
        declared = rpl.get("declared")
        verdict = rpl.get("verdict") or "?"
        obs = rpl.get("observed") or {}
        primary_age = obs.get("primary_age_s")
        threshold = obs.get("silent_fail_s")
        observed.append(("declared (upstream)", str(declared)))
        observed.append(("images in 1h window", str(obs.get("primary", "—"))))
        if primary_age is not None:
            observed.append(("newest scan age", f"{primary_age} s"))
        if threshold is not None:
            observed.append(("fresh threshold (per-radar)", f"{threshold} s"))
        dead = rpl.get("dead_moments") or []
        if dead:
            observed.append(("dead moments", ", ".join(dead)))
        observed.append(("verdict", verdict))
        if verdict == "GHOST_UP":
            what = (f"Upstream declares {target} as UP but no fresh scans are arriving. "
                    f"The newest image is {primary_age}s old; our threshold is "
                    f"{threshold}s. Either the radar briefly stopped or the feed is stalled.")
        elif verdict == "CONFIRMED_DOWN":
            what = f"Both upstream and our observation agree: {target} is not producing scans."
        elif verdict == "STUCK_DOWN_FLAG":
            what = (f"Upstream's status flag says {target} is DOWN, but fresh scans are "
                    f"still arriving. The declaration is stuck; the radar is operating.")
        elif verdict == "OBSERVED_API_ERROR":
            what = "The xbandRadarImages or radar-status endpoint didn't respond cleanly."

    # -------- L3 overlay parity -------------------------------------
    elif check_id.startswith("layer3."):
        verify_urls.append(("rendered dashboard", f"{_UPSTREAM}/public"))
        if rpl.get("overlay_ts"):
            observed.append(("UI overlay ts", str(rpl["overlay_ts"])))
        if rpl.get("api_ts"):
            observed.append(("API step[0] ts", str(rpl["api_ts"])))
        what = "The dashboard overlay timestamp diverged from the upstream API."

    # -------- L4 image QC -------------------------------------------
    elif check_id.startswith("layer4."):
        src = rpl.get("source")
        if src:
            verify_urls.append(("captured PNG (upstream)",
                                f"{_UPSTREAM}/api/imageData?file={quote(src, safe='/')}"))
            if SETTINGS.public_url:
                captured_image_url = (f"{SETTINGS.public_url}/api/upstream/image_by_source.png"
                                      f"?source={quote(src, safe='')}")
        t1 = rpl.get("tier1") or {}
        t2 = rpl.get("tier2") or {}
        prof = rpl.get("profile") or {}
        if "coverage_pct" in t1:
            observed.append(("pixel coverage", f"{t1['coverage_pct']}%"))
        for det in ("extreme", "speckle", "range_ring", "frozen"):
            v = (t2.get(det) or {}).get("verdict")
            if v:
                observed.append((f"{det} verdict", v))
        if prof:
            observed.append(("profile applied", ", ".join(f"{k}={v}" for k, v in prof.items())))
        what = ("Image-quality heuristics tripped on the latest scan — see the "
                "captured image link below to inspect it directly.")

    # -------- Transport-error override ----------------------------------
    # If the check raised an exception (status=error), the per-check-
    # family `what` above is wrong — no heuristic actually ran, the
    # check died trying to talk to the upstream. Override with a
    # transport-level explanation that names the exception class and
    # message so the operator can see at a glance whether this is a
    # DNS blip, an upstream outage, or our infrastructure.
    if status_at_open == "error":
        exc_name = rpl.get("exception") or "Exception"
        exc_msg = rpl.get("message") or alarm.get("message") or ""
        what = (
            f"The check failed before reaching the upstream data — "
            f"{exc_name}: {exc_msg}. No checks against the response "
            f"ran for this cycle. Most common causes: a transient DNS "
            f"hiccup or local-network blip, or an upstream service "
            f"outage. If the issue persists across consecutive cycles "
            f"and the URLs below are reachable from your browser, "
            f"escalate to upstream."
        )
        # Observed values from the per-check branch are meaningless on
        # an error — clear them so the email doesn't dangle a
        # partially-filled threshold table.
        observed = []
        # Replace any per-check URL suggestions with a small fixed set
        # the operator can quickly probe to localize the fault.
        verify_urls = [
            ("upstream reachability (radar-status)", f"{_UPSTREAM}/api/radar-status/"),
            ("upstream root", _UPSTREAM),
        ]

    # -------- Dashboard link --------------------------------------------
    dashboard_url = None
    if SETTINGS.public_url:
        dashboard_url = (f"{SETTINGS.public_url}/timeline"
                         f"?check={quote(check_id, safe='')}"
                         f"&target={quote(target, safe='')}")

    # One-line problem summary (for subject + email teaser)
    short = alarm.get("message") or ""
    # Trim long summaries: keep the first sentence-ish.
    short_line = short.split("\n")[0]
    if len(short_line) > 90:
        short_line = short_line[:87] + "…"

    return {
        "check_id":            check_id,
        "target":              target,
        "stage":               stage,                 # canonical ID (L0…L4-T1T2)
        "stage_descriptor":    _stage_descriptor(stage),  # what humans see
        "stage_tech":          _stage_tech(stage),    # short code for footer
        "severity":            severity,
        "severity_upper":      severity.upper(),
        "opened_at_str":       _opened_str(alarm.get("opened_at")),
        "opened_age":          _opened_age(alarm.get("opened_at")),
        "message":             alarm.get("message", ""),
        "short_line":          short_line,
        "what":                what,
        "observed":            observed,             # list of (label, value)
        "verify_urls":         verify_urls,          # list of (label, url)
        "captured_image_url":  captured_image_url,
        "dashboard_url":       dashboard_url,
        "step_idx":            step_idx,
        "policy":              route.policy if hasattr(route, "policy") else "",
        "alarm":               alarm,
    }


def _subject(ctx: dict) -> str:
    """Compact, scannable subject. Example:
       [SENTINEL WARN] Radar Scans/CBAND — newest scan 612s old (limit 480s)"""
    sev = ctx["severity_upper"]
    stage = ctx["stage_descriptor"]
    target = ctx["target"] or ctx["check_id"].split(".")[-1]
    short = ctx["short_line"]
    head = f"[SENTINEL {sev}] {stage}/{target}"
    if short:
        return f"{head} — {short}"
    return head


class EmailSink:
    name = "email"

    def __init__(self, smtp_cfg):
        if smtp_cfg is None:
            raise RuntimeError("email sink requires an 'smtp:' block in alerts.yaml")
        self.smtp = smtp_cfg

    async def send(self, alarm, receiver, route, step_idx) -> SinkResult:
        ctx = _build_ctx(alarm, route, step_idx)

        text_tpl = _env.get_template(f"{receiver.template}.txt")
        text_body = text_tpl.render(**ctx)

        # HTML is best-effort — fall back to text-only if no .html sibling exists.
        try:
            html_tpl = _env.get_template(f"{receiver.template}.html")
            html_body: str | None = html_tpl.render(**ctx)
        except Exception:
            html_body = None

        msg = EmailMessage()
        msg["From"] = self.smtp.from_
        msg["To"] = ", ".join(receiver.email)
        msg["Subject"] = _subject(ctx)
        if self.smtp.reply_to:
            msg["Reply-To"] = self.smtp.reply_to
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        try:
            await asyncio.wait_for(
                aiosmtplib.send(
                    msg,
                    hostname=self.smtp.host, port=self.smtp.port,
                    username=self.smtp.username, password=self.smtp.password,
                    start_tls=self.smtp.starttls,
                ),
                timeout=15,
            )
            return SinkResult(delivered=True, body_excerpt=text_body[:500])
        except Exception as e:
            return SinkResult(delivered=False, body_excerpt=text_body[:500], error=str(e))
