"""Media storage (uploads + generated post designs).

Files live under DATA_DIR/media. Each file has an unguessable public token so
platforms (Instagram, Facebook, TikTok) can fetch it by URL when publishing.
"""
from __future__ import annotations

import io
import mimetypes
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from app import config, db
from app.security import random_token

ALLOWED = {
    "image/jpeg": "image", "image/png": "image", "image/webp": "image",
    "video/mp4": "video", "video/quicktime": "video",
}
MAX_IMAGE = 15 * 1024 * 1024
MAX_VIDEO = 250 * 1024 * 1024

FONT_CANDIDATES = [
    str(config.ROOT / "app" / "assets" / "fonts" / "IBMPlexSansArabic-Bold.ttf"),
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_REGULAR_CANDIDATES = [
    str(config.ROOT / "app" / "assets" / "fonts" / "IBMPlexSansArabic-Regular.ttf"),
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


class MediaError(ValueError):
    pass


def media_path(row: dict) -> Path:
    return config.settings.media_dir / row["user_id"] / row["filename"]


def public_url(row: dict) -> str:
    ext = Path(row["filename"]).suffix
    return f"{config.settings.base_url}/m/{row['public_token']}{ext}"


def save(user_id: str, data: bytes, content_type: str, kind_hint: str = "upload") -> dict:
    kind = ALLOWED.get(content_type)
    if not kind:
        raise MediaError("unsupported_type")
    if kind == "image" and len(data) > MAX_IMAGE or kind == "video" and len(data) > MAX_VIDEO:
        raise MediaError("file_too_large")
    if kind == "image":
        # Normalise every image to JPEG (Instagram accepts JPEG only) and strip metadata.
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:  # noqa: BLE001
            raise MediaError("invalid_image") from exc
        img = img.convert("RGB")
        if max(img.size) > 2160:
            img.thumbnail((2160, 2160))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, optimize=True)
        data, content_type, ext = buf.getvalue(), "image/jpeg", ".jpg"
    else:
        ext = mimetypes.guess_extension(content_type) or ".mp4"
        if ext == ".qt":
            ext = ".mov"
    mid = db.new_id()
    row = {
        "id": mid, "user_id": user_id, "kind": kind, "filename": f"{mid}{ext}", "content_type": content_type,
        "size": len(data), "public_token": random_token(18), "created_at": db.now(),
    }
    path = media_path(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.insert("media", row)
    return row


def get_owned(user_id: str, media_ids: list[str]) -> list[dict]:
    rows = []
    for mid in media_ids:
        r = db.one("SELECT * FROM media WHERE id = ? AND user_id = ?", (mid, user_id))
        if r:
            rows.append(r)
    return rows


# ---- designed post images -------------------------------------------------

def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    layout = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size, layout_engine=layout)
    return ImageFont.load_default(size)


def _hex(c: str, fallback=(109, 61, 245)) -> tuple[int, int, int]:
    c = (c or "").lstrip("#")
    if len(c) == 6:
        try:
            return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    return fallback


def _is_rtl(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, direction: str) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if draw.textlength(trial, font=font, direction=direction) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return [line for line in lines if line is not None]


def design_post(user_id: str, headline: str, subline: str = "", footer: str = "", style: str = "bold", color: str = "#6d3df5") -> dict:
    """Render a 1080×1350 (4:5) branded post image and store it."""
    W, H = 1080, 1350
    brand = _hex(color)
    dark = (12, 12, 15)
    # soft radial brand glow, rendered small then upscaled (fast and smooth)
    sw, sh = 108, 135
    small = Image.new("RGB", (sw, sh), dark)
    spx = small.load()
    for yy in range(sh):
        for xx in range(sw):
            t = max(0.0, 1 - ((xx - sw * 0.85) ** 2 + (yy - sh * 0.05) ** 2) ** 0.5 / (sw * 1.1))
            if style == "minimal":
                t *= 0.35
            spx[xx, yy] = tuple(int(dark[i] + (brand[i] - dark[i]) * t * 0.75) for i in range(3))
    img = small.resize((W, H), Image.BICUBIC)
    d = ImageDraw.Draw(img)
    rtl = _is_rtl(headline + subline)
    direction = "rtl" if rtl and features.check("raqm") else None
    margin = 96
    # accent line (on the reading side)
    ax = W - margin - 120 if rtl else margin
    d.rectangle([ax, 150, ax + 120, 158], fill=brand)
    size = 104 if len(headline) < 40 else 84 if len(headline) < 80 else 68
    f_head = _font(FONT_CANDIDATES, size)
    f_sub = _font(FONT_REGULAR_CANDIDATES, 46)
    f_foot = _font(FONT_CANDIDATES, 38)
    if style == "offer":
        bx = W - margin - 300 if rtl else margin
        d.rounded_rectangle([bx, 230, bx + 300, 310], radius=40, fill=brand)
        d.text((bx + 150, 270), "عرض خاص" if rtl else "SPECIAL", font=_font(FONT_CANDIDATES, 40), fill="white", anchor="mm", direction=direction)
    y = 360 if style != "quote" else 420
    if style == "quote":
        d.text((W - margin if rtl else margin, 250), "”" if rtl else "“", font=_font(FONT_CANDIDATES, 220), fill=brand, anchor="ra" if rtl else "la")
    anchor = "ra" if rtl else "la"
    x = W - margin if rtl else margin
    for line in _wrap(d, headline, f_head, W - 2 * margin, direction or "ltr"):
        d.text((x, y), line, font=f_head, fill="white", anchor=anchor, direction=direction)
        y += int(size * 1.35)
    if subline:
        y += 30
        for line in _wrap(d, subline, f_sub, W - 2 * margin, direction or "ltr"):
            d.text((x, y), line, font=f_sub, fill=(201, 201, 212), anchor=anchor, direction=direction)
            y += 70
    d.line([margin, H - 170, W - margin, H - 170], fill=(42, 42, 51), width=2)
    if footer:
        d.text((x, H - 120), footer, font=f_foot, fill="white", anchor=anchor, direction=direction)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, optimize=True)
    return save(user_id, buf.getvalue(), "image/jpeg", "design")
