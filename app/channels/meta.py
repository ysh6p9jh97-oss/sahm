"""Facebook Pages + Instagram (professional accounts) via the Graph API.

Connect flow: Facebook Login → long-lived user token → Page tokens (Page
tokens derived from a long-lived user token do not expire) → each Page and
its linked Instagram professional account becomes a channel.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

from app import config
from app.channels import ChannelError, http

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "pages_manage_engagement",
    "read_insights",
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
]


def graph(path: str) -> str:
    return f"https://graph.facebook.com/{config.settings.meta_graph_version}/{path.lstrip('/')}"


def configured() -> bool:
    return bool(config.settings.meta_app_id and config.settings.meta_app_secret)


def redirect_uri() -> str:
    return f"{config.settings.base_url}/api/channels/meta/callback"


def authorize_url(state: str) -> str:
    q = urlencode({
        "client_id": config.settings.meta_app_id,
        "redirect_uri": redirect_uri(),
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    })
    return f"https://www.facebook.com/{config.settings.meta_graph_version}/dialog/oauth?{q}"


def exchange_code(code: str) -> str:
    s = config.settings
    short = http("GET", graph("oauth/access_token"), params={
        "client_id": s.meta_app_id, "client_secret": s.meta_app_secret,
        "redirect_uri": redirect_uri(), "code": code,
    })["access_token"]
    long = http("GET", graph("oauth/access_token"), params={
        "grant_type": "fb_exchange_token", "client_id": s.meta_app_id,
        "client_secret": s.meta_app_secret, "fb_exchange_token": short,
    })
    return long["access_token"]


def list_pages(user_token: str) -> list[dict]:
    data = http("GET", graph("me/accounts"), params={
        "access_token": user_token,
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "limit": 100,
    })
    return data.get("data", [])


# ---- publishing -------------------------------------------------------------

def publish_facebook(page_id: str, page_token: str, caption: str, media: list[dict]) -> dict:
    """media: [{'url': public_url, 'kind': 'image'|'video'}]"""
    if not media:
        r = http("POST", graph(f"{page_id}/feed"), data={"message": caption, "access_token": page_token})
        return {"id": r.get("id")}
    first = media[0]
    if first["kind"] == "video":
        r = http("POST", graph(f"{page_id}/videos"), data={"file_url": first["url"], "description": caption, "access_token": page_token})
        return {"id": r.get("id")}
    if len(media) == 1:
        r = http("POST", graph(f"{page_id}/photos"), data={"url": first["url"], "caption": caption, "access_token": page_token})
        return {"id": r.get("post_id") or r.get("id")}
    attached = []
    for m in media[:10]:
        r = http("POST", graph(f"{page_id}/photos"), data={"url": m["url"], "published": "false", "access_token": page_token})
        attached.append({"media_fbid": r["id"]})
    import json
    r = http("POST", graph(f"{page_id}/feed"), data={"message": caption, "attached_media": json.dumps(attached), "access_token": page_token})
    return {"id": r.get("id")}


def _wait_container(container_id: str, token: str, attempts: int = 30, delay: float = 6.0) -> None:
    for _ in range(attempts):
        st = http("GET", graph(container_id), params={"fields": "status_code", "access_token": token}).get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise ChannelError(f"Instagram media processing {st}")
        time.sleep(delay)
    raise ChannelError("Instagram media processing timed out")


def publish_instagram(ig_id: str, token: str, caption: str, media: list[dict]) -> dict:
    if not media:
        raise ChannelError("Instagram requires at least one image or video")
    if len(media) == 1:
        m = media[0]
        data = {"caption": caption, "access_token": token}
        if m["kind"] == "video":
            data.update({"media_type": "REELS", "video_url": m["url"]})
        else:
            data["image_url"] = m["url"]
        cid = http("POST", graph(f"{ig_id}/media"), data=data)["id"]
        _wait_container(cid, token, delay=6.0 if m["kind"] == "video" else 2.0)
    else:
        children = []
        for m in media[:10]:
            d = {"is_carousel_item": "true", "access_token": token}
            if m["kind"] == "video":
                d.update({"media_type": "VIDEO", "video_url": m["url"]})
            else:
                d["image_url"] = m["url"]
            child = http("POST", graph(f"{ig_id}/media"), data=d)["id"]
            _wait_container(child, token, delay=3.0)
            children.append(child)
        cid = http("POST", graph(f"{ig_id}/media"), data={
            "media_type": "CAROUSEL", "children": ",".join(children), "caption": caption, "access_token": token,
        })["id"]
        _wait_container(cid, token, delay=2.0)
    r = http("POST", graph(f"{ig_id}/media_publish"), data={"creation_id": cid, "access_token": token})
    return {"id": r.get("id")}


# ---- insights & comments ----------------------------------------------------

def facebook_post_stats(post_id: str, token: str) -> dict:
    d = http("GET", graph(post_id), params={
        "fields": "likes.summary(true).limit(0),comments.summary(true).limit(0),shares,permalink_url",
        "access_token": token,
    })
    return {
        "likes": d.get("likes", {}).get("summary", {}).get("total_count", 0),
        "comments": d.get("comments", {}).get("summary", {}).get("total_count", 0),
        "shares": d.get("shares", {}).get("count", 0),
        "url": d.get("permalink_url"),
    }


def instagram_media_stats(media_id: str, token: str) -> dict:
    d = http("GET", graph(media_id), params={"fields": "like_count,comments_count,permalink", "access_token": token})
    out = {"likes": d.get("like_count", 0), "comments": d.get("comments_count", 0), "url": d.get("permalink")}
    try:
        ins = http("GET", graph(f"{media_id}/insights"), params={"metric": "reach,saved,shares", "access_token": token})
        for item in ins.get("data", []):
            vals = item.get("values") or [{}]
            out[item["name"]] = vals[0].get("value", item.get("total_value", {}).get("value", 0))
    except ChannelError:
        pass  # insights may be unavailable for some media types; basic counts still return
    return out


def list_comments(platform: str, object_id: str, token: str, limit: int = 25) -> list[dict]:
    if platform == "instagram":
        d = http("GET", graph(f"{object_id}/comments"), params={"fields": "id,text,username,timestamp", "limit": limit, "access_token": token})
        return [{"id": c["id"], "text": c.get("text", ""), "author": c.get("username", ""), "at": c.get("timestamp")} for c in d.get("data", [])]
    d = http("GET", graph(f"{object_id}/comments"), params={"fields": "id,message,from,created_time", "limit": limit, "access_token": token})
    return [{"id": c["id"], "text": c.get("message", ""), "author": (c.get("from") or {}).get("name", ""), "at": c.get("created_time")} for c in d.get("data", [])]


def reply_comment(platform: str, comment_id: str, token: str, message: str) -> dict:
    if platform == "instagram":
        r = http("POST", graph(f"{comment_id}/replies"), data={"message": message, "access_token": token})
    else:
        r = http("POST", graph(f"{comment_id}/comments"), data={"message": message, "access_token": token})
    return {"id": r.get("id")}
