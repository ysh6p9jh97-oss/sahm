"""Sari's websites: build, revise, publish and download.

Published pages are served with a CSP sandbox so they run in an opaque
origin and can never read SAHM's cookies. Contact forms inside a page post to
/f/<slug> and land in the client's contacts (and can trigger automations).
"""
from __future__ import annotations

import io
import re
import zipfile

from app import billing, config, db
from app.security import random_token

MAX_HTML = 400_000


def _slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30]
    return f"{base or 'site'}-{random_token(4).lower().replace('_', '').replace('-', '')[:6]}"


def _clean(html: str) -> str:
    html = (html or "").strip()
    if len(html) > MAX_HTML:
        raise ValueError("site_too_large")
    if "<html" not in html.lower():
        html = f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>{html}</body></html>'
    return html


def create(user_id: str, title: str, html: str) -> dict:
    row = {"id": db.new_id(), "user_id": user_id, "slug": _slugify(title), "title": title[:160] or "موقع", "html": _clean(html),
           "status": "draft", "version": 1, "published_html": None, "published_at": None, "created_at": db.now(), "updated_at": db.now()}
    db.insert("sites", row)
    db.log_activity(user_id, "site", f"إنشاء موقع: {row['title']}", row["id"])
    return row


def update(user_id: str, site_id: str, html: str, title: str | None = None) -> dict:
    site = get(user_id, site_id)
    if not site:
        raise ValueError("site_not_found")
    vals = {"html": _clean(html), "version": site["version"] + 1, "updated_at": db.now()}
    if title:
        vals["title"] = title[:160]
    db.update("sites", {"id": site_id}, vals)
    return get(user_id, site_id)


def get(user_id: str, site_id: str) -> dict | None:
    return db.one("SELECT * FROM sites WHERE id = ? AND user_id = ?", (site_id, user_id))


def publish(user_id: str, site_id: str) -> dict:
    site = get(user_id, site_id)
    if not site:
        raise ValueError("site_not_found")
    if site["status"] != "published":
        billing.check(user_id, "sites_published")
    db.update("sites", {"id": site_id}, {"status": "published", "published_html": site["html"], "published_at": db.now(), "updated_at": db.now()})
    db.log_activity(user_id, "site", f"نشر موقع: {site['title']}", site_id)
    return get(user_id, site_id)


def unpublish(user_id: str, site_id: str) -> None:
    db.run("UPDATE sites SET status = 'draft', updated_at = ? WHERE id = ? AND user_id = ?", (db.now(), site_id, user_id))


def public_url(site: dict) -> str:
    return f"{config.settings.public_sites_base}/{site['slug']}"


def to_public(site: dict, with_html: bool = False) -> dict:
    out = {"id": site["id"], "title": site["title"], "slug": site["slug"], "status": site["status"], "version": site["version"],
           "url": public_url(site), "published_at": site["published_at"], "updated_at": site["updated_at"],
           "form_endpoint": f"{config.settings.base_url}/f/{site['slug']}"}
    if with_html:
        out["html"] = site["html"]
    return out


def zip_bytes(site: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", site["html"])
        z.writestr("README.txt", f"{site['title']}\n\nملف index.html جاهز للرفع على أي استضافة (Netlify, Vercel, GitHub Pages).\n"
                   f"النموذج داخل الصفحة يرسل إلى: {config.settings.base_url}/f/{site['slug']}\n")
    return buf.getvalue()


SANDBOX_CSP = "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals"
