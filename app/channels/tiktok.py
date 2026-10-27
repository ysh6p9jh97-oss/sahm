"""TikTok Login Kit (OAuth v2) + Content Posting API (Direct Post).

Until the TikTok app passes TikTok's audit, TikTok only allows posting to
private accounts / private visibility. The code surfaces that error as-is.
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlencode

from app import config
from app.channels import ChannelError, http

API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish"


def configured() -> bool:
    return bool(config.settings.tiktok_client_key and config.settings.tiktok_client_secret)


def redirect_uri() -> str:
    return f"{config.settings.base_url}/api/channels/tiktok/callback"


def authorize_url(state: str) -> str:
    q = urlencode({
        "client_key": config.settings.tiktok_client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "state": state,
    })
    return f"https://www.tiktok.com/v2/auth/authorize/?{q}"


def _token_request(data: dict) -> dict:
    s = config.settings
    body = {"client_key": s.tiktok_client_key, "client_secret": s.tiktok_client_secret, **data}
    r = http("POST", f"{API}/oauth/token/", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if "access_token" not in r:
        raise ChannelError(str(r.get("error_description") or r))
    r["obtained_at"] = int(time.time())
    return r


def exchange_code(code: str) -> dict:
    return _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})


def refresh(refresh_token: str) -> dict:
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def user_info(token: str) -> dict:
    r = http("GET", f"{API}/user/info/", params={"fields": "open_id,display_name,avatar_url"}, headers={"Authorization": f"Bearer {token}"})
    return r.get("data", {}).get("user", {})


def _post(path: str, token: str, body: dict) -> dict:
    r = http("POST", f"{API}{path}", json=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    err = r.get("error") or {}
    if err.get("code") not in (None, "ok"):
        raise ChannelError(f"{err.get('code')}: {err.get('message')}")
    return r.get("data", {})


def creator_info(token: str) -> dict:
    return _post("/post/publish/creator_info/query/", token, {})


def _privacy(token: str) -> str:
    options = creator_info(token).get("privacy_level_options") or []
    for preferred in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"):
        if preferred in options:
            return preferred
    return "SELF_ONLY"


def publish_video_file(token: str, caption: str, path: Path) -> dict:
    size = path.stat().st_size
    data = _post("/post/publish/video/init/", token, {
        "post_info": {"title": caption[:2200], "privacy_level": _privacy(token), "is_aigc": False},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1},
    })
    upload_url = data["upload_url"]
    with path.open("rb") as fh:
        http("PUT", upload_url, data=fh.read(), headers={
            "Content-Type": "video/mp4", "Content-Range": f"bytes 0-{size - 1}/{size}", "Content-Length": str(size),
        }, timeout=300)
    return {"id": data.get("publish_id")}


def publish_photos(token: str, caption: str, image_urls: list[str]) -> dict:
    """Photo posts pull images from URLs on a domain verified in the TikTok developer portal."""
    data = _post("/post/publish/content/init/", token, {
        "post_info": {"title": caption[:90], "description": caption[:4000], "privacy_level": _privacy(token)},
        "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": image_urls[:35]},
        "post_mode": "DIRECT_POST",
        "media_type": "PHOTO",
    })
    return {"id": data.get("publish_id")}


def publish_status(token: str, publish_id: str) -> dict:
    return _post("/post/publish/status/fetch/", token, {"publish_id": publish_id})
