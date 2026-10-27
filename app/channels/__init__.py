"""Connections to external platforms. Each module talks to the official API
of one platform; nothing here pretends to succeed — every call either returns
the platform's real response or raises ChannelError with its message.
"""
from __future__ import annotations

from typing import Any

import requests


class ChannelError(Exception):
    pass


def http(method: str, url: str, **kw: Any) -> Any:
    kw.setdefault("timeout", 30)
    try:
        resp = requests.request(method, url, **kw)
    except requests.RequestException as exc:
        raise ChannelError(f"connection failed: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        msg = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("description") or msg
        raise ChannelError(f"{resp.status_code}: {msg}")
    return data


PLATFORM_LABELS = {
    "facebook": "فيسبوك",
    "instagram": "إنستقرام",
    "tiktok": "تيك توك",
    "telegram": "تيليجرام",
    "whatsapp": "واتساب",
    "snapchat": "سناب شات",
    "n8n": "n8n",
}
