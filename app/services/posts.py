"""Post lifecycle for Sulaf.

draft → pending_approval → scheduled → publishing → published | partial | failed
A post reaches 'scheduled' only through the client's approval.
"""
from __future__ import annotations

import time

from app import billing, db, media
from app.channels import ChannelError, meta as meta_api, telegram, tiktok
from app.services import channels as ch

PUBLISHABLE = ("facebook", "instagram", "tiktok", "telegram")


def create(user_id: str, caption: str, media_ids: list[str], channel_ids: list[str], scheduled_at: int | None) -> dict:
    owned_media = [m["id"] for m in media.get_owned(user_id, media_ids)]
    owned_channels = [c for c in channel_ids if ch.get(user_id, c)]
    row = {
        "id": db.new_id(), "user_id": user_id, "caption": caption.strip()[:5000], "media_ids": db.jdump(owned_media),
        "channel_ids": db.jdump(owned_channels), "status": "draft", "scheduled_at": scheduled_at, "published_at": None,
        "results": "{}", "insights": "{}", "insights_at": None, "created_at": db.now(), "updated_at": db.now(),
    }
    db.insert("posts", row)
    return row


def get(user_id: str, post_id: str) -> dict | None:
    return db.one("SELECT * FROM posts WHERE id = ? AND user_id = ?", (post_id, user_id))


def to_public(row: dict) -> dict:
    media_rows = media.get_owned(row["user_id"], db.jload(row["media_ids"], []))
    chans = [ch.get(row["user_id"], c) for c in db.jload(row["channel_ids"], [])]
    return {
        "id": row["id"], "caption": row["caption"], "status": row["status"], "scheduled_at": row["scheduled_at"],
        "published_at": row["published_at"], "results": db.jload(row["results"], {}), "insights": db.jload(row["insights"], {}),
        "media": [{"id": m["id"], "kind": m["kind"], "url": media.public_url(m)} for m in media_rows],
        "channels": [ch.public(c) for c in chans if c],
        "created_at": row["created_at"],
    }


def approve(user_id: str, post_id: str) -> dict:
    post = get(user_id, post_id)
    if not post:
        raise ValueError("post_not_found")
    if not db.jload(post["channel_ids"], []):
        raise ValueError("no_channels")
    when = post["scheduled_at"] or db.now()
    db.update("posts", {"id": post_id}, {"status": "scheduled", "scheduled_at": when, "updated_at": db.now()})
    db.log_activity(user_id, "post", "اعتماد منشور للنشر", post_id)
    return get(user_id, post_id)


def publish(post: dict) -> dict:
    """Publish to every selected channel. Returns per-channel results."""
    user_id = post["user_id"]
    claimed = db.run("UPDATE posts SET status = 'publishing', updated_at = ? WHERE id = ? AND status = 'scheduled'", (db.now(), post["id"]))
    if not claimed:
        return db.jload(post["results"], {})
    try:
        billing.check(user_id, "posts")
    except billing.LimitReached:
        db.update("posts", {"id": post["id"]}, {"status": "failed", "results": db.jdump({"_": {"ok": False, "error": "limit_reached"}}), "updated_at": db.now()})
        db.notify(user_id, "لم يُنشر منشور", "وصلت لحد المنشورات في باقتك الحالية.", "#/billing")
        return {}
    media_rows = media.get_owned(user_id, db.jload(post["media_ids"], []))
    items = [{"kind": m["kind"], "url": media.public_url(m), "path": str(media.media_path(m))} for m in media_rows]
    results: dict = {}
    for cid in db.jload(post["channel_ids"], []):
        row = ch.get(user_id, cid)
        if not row:
            continue
        try:
            results[cid] = {"ok": True, "platform": row["platform"], **_publish_one(row, post["caption"], items)}
        except (ChannelError, ValueError) as exc:
            results[cid] = {"ok": False, "platform": row["platform"], "error": str(exc)[:400]}
            ch.mark_error(row, str(exc))
    ok = [r for r in results.values() if r["ok"]]
    status = "published" if ok and len(ok) == len(results) else "partial" if ok else "failed"
    db.update("posts", {"id": post["id"]}, {"status": status, "results": db.jdump(results), "published_at": db.now() if ok else None, "updated_at": db.now()})
    if ok:
        billing.consume(user_id, "posts")
    title = {"published": "تم نشر منشورك", "partial": "نُشر منشورك جزئيًا", "failed": "تعذّر نشر منشورك"}[status]
    db.notify(user_id, title, post["caption"][:120], "#/studio")
    db.log_activity(user_id, "post", title, post["id"])
    return results


def _publish_one(row: dict, caption: str, items: list[dict]) -> dict:
    platform, token, m = row["platform"], ch.secret(row), ch.meta(row)
    if platform == "facebook":
        return meta_api.publish_facebook(row["external_id"], token, caption, items)
    if platform == "instagram":
        return meta_api.publish_instagram(row["external_id"], token, caption, items)
    if platform == "telegram":
        return telegram.send_media(token, m["chat_id"], caption, items)
    if platform == "tiktok":
        token = _fresh_tiktok_token(row)
        videos = [i for i in items if i["kind"] == "video"]
        if videos:
            from pathlib import Path
            return tiktok.publish_video_file(token, caption, Path(videos[0]["path"]))
        images = [i["url"] for i in items if i["kind"] == "image"]
        if not images:
            raise ValueError("TikTok requires a video or images")
        return tiktok.publish_photos(token, caption, images)
    raise ValueError(f"publishing is not supported for {platform}")


def _fresh_tiktok_token(row: dict) -> str:
    data = db.jload(ch.secret(row), {})
    if data.get("obtained_at", 0) + int(data.get("expires_in", 0)) - 300 > time.time():
        return data["access_token"]
    new = tiktok.refresh(data["refresh_token"])
    ch.upsert(row["user_id"], "tiktok", row["external_id"], row["name"], db.jdump(new), ch.meta(row))
    return new["access_token"]


def refresh_insights(post: dict) -> dict:
    stats: dict = {}
    for cid, res in db.jload(post["results"], {}).items():
        if not res.get("ok") or not res.get("id"):
            continue
        row = ch.get(post["user_id"], cid)
        if not row:
            continue
        try:
            if row["platform"] == "facebook":
                stats[cid] = {"platform": "facebook", **meta_api.facebook_post_stats(res["id"], ch.secret(row))}
            elif row["platform"] == "instagram":
                stats[cid] = {"platform": "instagram", **meta_api.instagram_media_stats(res["id"], ch.secret(row))}
        except ChannelError as exc:
            stats[cid] = {"platform": row["platform"], "error": str(exc)[:200]}
    db.update("posts", {"id": post["id"]}, {"insights": db.jdump(stats), "insights_at": db.now()})
    return stats
