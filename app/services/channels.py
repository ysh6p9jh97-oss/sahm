"""Stored channel connections (secrets encrypted at rest)."""
from __future__ import annotations

from app import billing, db
from app.security import decrypt, encrypt


def upsert(user_id: str, platform: str, external_id: str, name: str, secret: str, meta: dict) -> dict:
    existing = db.one("SELECT * FROM channels WHERE user_id = ? AND platform = ? AND external_id = ?", (user_id, platform, external_id))
    if existing:
        db.update("channels", {"id": existing["id"]}, {"name": name, "secret": encrypt(secret), "meta": db.jdump(meta), "status": "connected", "last_error": None})
        return db.one("SELECT * FROM channels WHERE id = ?", (existing["id"],))
    billing.check(user_id, "channels")
    row = {
        "id": db.new_id(), "user_id": user_id, "platform": platform, "external_id": external_id, "name": name,
        "secret": encrypt(secret), "meta": db.jdump(meta), "status": "connected", "last_error": None, "created_at": db.now(),
    }
    db.insert("channels", row)
    db.log_activity(user_id, "channel", f"ربط حساب {platform}: {name}", row["id"])
    return row


def get(user_id: str, channel_id: str) -> dict | None:
    return db.one("SELECT * FROM channels WHERE id = ? AND user_id = ?", (channel_id, user_id))


def list_for(user_id: str, platform: str | None = None) -> list[dict]:
    if platform:
        return db.all_("SELECT * FROM channels WHERE user_id = ? AND platform = ? ORDER BY created_at", (user_id, platform))
    return db.all_("SELECT * FROM channels WHERE user_id = ? ORDER BY created_at", (user_id,))


def secret(row: dict) -> str:
    return decrypt(row["secret"])


def meta(row: dict) -> dict:
    return db.jload(row["meta"], {})


def public(row: dict) -> dict:
    return {
        "id": row["id"], "platform": row["platform"], "name": row["name"], "status": row["status"],
        "last_error": row["last_error"], "created_at": row["created_at"],
        "meta": {k: v for k, v in meta(row).items() if k in ("username", "page_name", "display_phone", "bot_username", "chat_title", "base_url", "kind")},
    }


def mark_error(row: dict, error: str) -> None:
    db.update("channels", {"id": row["id"]}, {"last_error": error[:500]})
