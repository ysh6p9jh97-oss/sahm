"""Small helpers shared by routes and tools."""
from __future__ import annotations

import hashlib
import hmac

from app import config
from app.security import _signing_key


def calendar_token(user_id: str) -> str:
    return hmac.new(_signing_key().encode(), f"cal:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]


def calendar_url(user: dict, project_id: str | None = None) -> str:
    url = f"{config.settings.base_url}/cal/{user['id']}/{calendar_token(user['id'])}.ics"
    return f"{url}?project={project_id}" if project_id else url
