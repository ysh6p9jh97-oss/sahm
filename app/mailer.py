"""Outgoing email over SMTP (works with Resend, Amazon SES, Zoho, Google Workspace…)."""
from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from app import config


class MailNotConfigured(Exception):
    pass


def configured() -> bool:
    return bool(config.settings.smtp_host)


def send(to: str, subject: str, text: str) -> str:
    s = config.settings
    if not s.smtp_host:
        raise MailNotConfigured("SMTP_HOST is not set")
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject[:200]
    msg["Message-ID"] = make_msgid(domain="sahm")
    msg.set_content(text)
    body = "<br>".join(html.escape(line) for line in text.splitlines())
    msg.add_alternative(
        f'<div dir="auto" style="font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.8;color:#111">{body}'
        f'<hr style="border:0;border-top:1px solid #ddd;margin:24px 0"><small style="color:#777">SAHM | شهم — منصة عالمية تخدمك وتسندك</small></div>',
        subtype="html",
    )
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=30) as smtp:
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ctx)
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    return msg["Message-ID"]
