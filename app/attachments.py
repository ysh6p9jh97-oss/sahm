"""Private chat attachments: safe storage and extraction for SAHM conversations."""
from __future__ import annotations

import base64
import csv
import io
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image, UnidentifiedImageError

from app import config, db

MAX_FILES = 5
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_TOTAL_SIZE = 16 * 1024 * 1024
MAX_TEXT_CHARS = 30_000

# Extension is authoritative; the browser-supplied MIME type is not trusted.
SUPPORTED: dict[str, tuple[str, str]] = {
    ".jpg": ("image/jpeg", "image"), ".jpeg": ("image/jpeg", "image"),
    ".png": ("image/png", "image"), ".webp": ("image/webp", "image"),
    ".pdf": ("application/pdf", "pdf"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "text"),
    ".txt": ("text/plain", "text"), ".md": ("text/markdown", "text"), ".csv": ("text/csv", "text"),
}


class AttachmentError(ValueError):
    """An attachment is unsupported or invalid."""


def _safe_name(value: str) -> str:
    name = Path((value or "file").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(" .")
    return (name or "file")[:180]


def storage_path(user_id: str, attachment: dict[str, Any]) -> Path:
    aid = str(attachment.get("id", ""))
    ext = str(attachment.get("storage_ext", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", aid) or ext not in {".jpg", ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv"}:
        raise AttachmentError("ملف المرفق غير صالح.")
    if not re.fullmatch(r"[0-9a-f]{32}", str(user_id)):
        raise AttachmentError("مسار المرفق غير صالح.")
    return config.settings.data_dir / "attachments" / user_id / f"{aid}{ext}"


def _xml_text(raw: bytes) -> str:
    root = ET.fromstring(raw)
    return "\n".join(x.strip() for x in root.itertext() if x and x.strip())


def _extract_office(ext: str, data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if ext == ".docx":
                name = "word/document.xml"
                if name not in zf.namelist():
                    raise AttachmentError("ملف Word غير صالح أو غير مدعوم.")
                root = ET.fromstring(zf.read(name))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paras = []
                for p in root.findall(".//w:p", ns):
                    line = "".join((n.text or "") for n in p.findall(".//w:t", ns))
                    if line.strip():
                        paras.append(line)
                return "\n".join(paras)
            if ext == ".xlsx":
                names = set(zf.namelist())
                shared: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    shared = ["".join(t.text or "" for t in node.iter() if t.tag.endswith("}t")) for node in root if node.tag.endswith("}si")]
                output: list[str] = []
                for name in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
                    root = ET.fromstring(zf.read(name))
                    for row in root.iter():
                        if not row.tag.endswith("}row"):
                            continue
                        vals: list[str] = []
                        for cell in list(row):
                            if not cell.tag.endswith("}c"):
                                continue
                            typ = cell.attrib.get("t")
                            val = next((x.text or "" for x in cell if x.tag.endswith("}v")), "")
                            if typ == "s" and val.isdigit() and int(val) < len(shared):
                                val = shared[int(val)]
                            elif typ == "inlineStr":
                                val = "".join(x.text or "" for x in cell.iter() if x.tag.endswith("}t"))
                            vals.append(val)
                        output.append(" | ".join(vals))
                return "\n".join(output)
            if ext == ".pptx":
                output = []
                for name in sorted(n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)):
                    output.append(_xml_text(zf.read(name)))
                return "\n\n".join(output)
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AttachmentError("تعذّر قراءة الملف. تأكدي أنه ملف Office صالح وغير محمي بكلمة مرور.") from exc
    raise AttachmentError("نوع الملف غير مدعوم.")


def prepare_upload(user_id: str, filename: str, content_type: str, data: bytes) -> dict[str, Any]:
    if not data:
        raise AttachmentError("الملف فارغ.")
    if len(data) > MAX_FILE_SIZE:
        raise AttachmentError("حجم كل ملف يجب ألا يتجاوز 8 ميغابايت.")
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise AttachmentError("الأنواع المدعومة: الصور JPG وPNG وWebP، وPDF وWord وExcel وPowerPoint وTXT وCSV.")
    canonical_type, kind = SUPPORTED[ext]
    extracted = ""
    stored_ext = ext
    if kind == "image":
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 40_000_000:
                    raise AttachmentError("أبعاد الصورة كبيرة جدًا.")
                image.load()
                image = image.convert("RGB")
                image.thumbnail((2500, 2500))
                out = io.BytesIO()
                image.save(out, format="JPEG", quality=88, optimize=True)
                data = out.getvalue()
        except AttachmentError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AttachmentError("الصورة غير صالحة أو غير مدعومة.") from exc
        canonical_type, stored_ext = "image/jpeg", ".jpg"
    elif ext == ".pdf":
        if not data.lstrip().startswith(b"%PDF-"):
            raise AttachmentError("ملف PDF غير صالح.")
        # PDFs are passed to the model as a PDF document content block.
    elif ext in (".docx", ".xlsx", ".pptx"):
        extracted = _extract_office(ext, data)[:MAX_TEXT_CHARS]
        if not extracted.strip():
            extracted = "لم يُستخرج نص من الملف؛ قد يكون فارغًا أو يحتوي على صور فقط."
    else:
        try:
            extracted = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                extracted = data.decode("cp1256")
            except UnicodeDecodeError:
                extracted = data.decode("latin-1", errors="replace")
        extracted = extracted.replace("\x00", "")[:MAX_TEXT_CHARS]
    aid = db.new_id()
    row = {
        "id": aid, "filename": name, "content_type": canonical_type, "size": len(data),
        "kind": kind, "storage_ext": stored_ext, "extracted_text": extracted,
    }
    path = storage_path(user_id, row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return row


def public_metadata(attachment: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    return {
        "id": attachment["id"], "filename": attachment["filename"],
        "content_type": attachment["content_type"], "size": attachment["size"],
        "kind": attachment.get("kind", "text"),
        "url": f"/api/conversations/{conversation_id}/attachments/{attachment['id']}",
    }


def model_blocks(user_id: str, attachments: list[dict[str, Any]], include_binary: bool = True) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in attachments:
        name = str(item.get("filename", "ملف مرفق"))
        kind = item.get("kind")
        if kind in ("image", "pdf") and include_binary:
            try:
                data = storage_path(user_id, item).read_bytes()
            except OSError:
                blocks.append({"type": "text", "text": f"تعذّر تحميل المرفق السابق: {name}"})
                continue
            encoded = base64.b64encode(data).decode("ascii")
            if kind == "image":
                blocks.append({"type": "text", "text": f"صورة مرفقة من العميل: {name}"})
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded}})
            else:
                blocks.append({"type": "text", "text": f"ملف PDF مرفق من العميل: {name}"})
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}})
        else:
            extracted = str(item.get("extracted_text", "")).strip()
            if extracted:
                blocks.append({"type": "text", "text": f"محتوى الملف المرفق ({name}):\n{extracted}"})
            else:
                blocks.append({"type": "text", "text": f"أرفق العميل ملفًا باسم {name}."})
    return blocks


def cleanup(user_id: str, attachment: dict[str, Any]) -> None:
    try:
        storage_path(user_id, attachment).unlink(missing_ok=True)
    except (OSError, AttachmentError):
        pass
