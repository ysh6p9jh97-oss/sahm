"""Telegram Bot API: post to channels/groups the bot administers, run a
customer-service bot through a webhook.
"""
from __future__ import annotations

from pathlib import Path

from app.channels import ChannelError, http


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, **kw) -> dict:
    r = http("POST", _api(token, method), **kw)
    if not r.get("ok"):
        raise ChannelError(r.get("description", "telegram error"))
    return r.get("result", {})


def get_me(token: str) -> dict:
    return _call(token, "getMe")


def get_chat(token: str, chat_id: str) -> dict:
    return _call(token, "getChat", data={"chat_id": chat_id})


def send_message(token: str, chat_id: str, text: str) -> dict:
    r = _call(token, "sendMessage", data={"chat_id": chat_id, "text": text[:4096]})
    return {"id": str(r.get("message_id"))}


def send_media(token: str, chat_id: str, caption: str, media: list[dict]) -> dict:
    """media: [{'kind','path'}] — uploaded directly, no public URL needed."""
    if not media:
        return send_message(token, chat_id, caption)
    m = media[0]
    method, field = ("sendVideo", "video") if m["kind"] == "video" else ("sendPhoto", "photo")
    with Path(m["path"]).open("rb") as fh:
        r = _call(token, method, data={"chat_id": chat_id, "caption": caption[:1024]}, files={field: fh}, timeout=300)
    if len(caption) > 1024:
        send_message(token, chat_id, caption[1024:])
    return {"id": str(r.get("message_id"))}


def set_webhook(token: str, url: str, secret: str) -> None:
    _call(token, "setWebhook", data={"url": url, "secret_token": secret, "allowed_updates": '["message"]'})


def delete_webhook(token: str) -> None:
    _call(token, "deleteWebhook")
