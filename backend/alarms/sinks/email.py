"""Email sink — aiosmtplib + Jinja2 plain-text template.

The template lives at ``backend/alarms/templates/<name>.txt``. If SMTP isn't
configured (``smtp:`` missing in alerts.yaml) the sink raises in __init__,
which the loader treats as "channel unavailable, log and skip".
"""
from __future__ import annotations
import asyncio
import logging
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .base import SinkResult

log = logging.getLogger(__name__)

_TPL_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TPL_DIR)),
    autoescape=select_autoescape(disabled_extensions=("txt",), default=False),
)


class EmailSink:
    name = "email"

    def __init__(self, smtp_cfg):
        if smtp_cfg is None:
            raise RuntimeError("email sink requires an 'smtp:' block in alerts.yaml")
        self.smtp = smtp_cfg

    async def send(self, alarm, receiver, route, step_idx) -> SinkResult:
        tpl = _env.get_template(f"{receiver.template}.txt")
        body = tpl.render(
            alarm=alarm,
            severity=alarm["severity"].upper(),
            stage=alarm["stage"],
            target=alarm["target"],
            check_id=alarm["check_id"],
            message=alarm["message"],
            opened_at=alarm["opened_at"],
            step_idx=step_idx,
            policy=route.policy if hasattr(route, "policy") else "",
        )
        subject = (
            f"[SENTINEL {alarm['severity'].upper()}] "
            f"{alarm['stage']}  {alarm['check_id']}  {alarm['message'][:60]}"
        )

        msg = EmailMessage()
        msg["From"] = self.smtp.from_
        msg["To"] = ", ".join(receiver.email)
        msg["Subject"] = subject
        if self.smtp.reply_to:
            msg["Reply-To"] = self.smtp.reply_to
        msg.set_content(body)

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
            return SinkResult(delivered=True, body_excerpt=body[:500])
        except Exception as e:
            return SinkResult(delivered=False, body_excerpt=body[:500], error=str(e))
