"""WhatsApp Business Cloud API (Meta).

The client connects their own WhatsApp Business number (Phone Number ID +
a permanent System User token from Meta Business Manager). Rules enforced by
WhatsApp: free-form replies only within 24h of the customer's last message;
outside that window only pre-approved templates can be sent, and only to
customers who opted in.
"""
from __future__ import annotations

from app import config
from app.channels import http


def _url(path: str) -> str:
    return f"https://graph.facebook.com/{config.settings.meta_graph_version}/{path}"


def verify_number(phone_number_id: str, token: str) -> dict:
    return http("GET", _url(phone_number_id), params={"fields": "display_phone_number,verified_name", "access_token": token})


def send_text(phone_number_id: str, token: str, to: str, text: str) -> dict:
    r = http("POST", _url(f"{phone_number_id}/messages"), headers={"Authorization": f"Bearer {token}"}, json={
        "messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096], "preview_url": False},
    })
    return {"id": (r.get("messages") or [{}])[0].get("id")}


def send_template(phone_number_id: str, token: str, to: str, template: str, lang: str = "ar", params: list[str] | None = None) -> dict:
    tpl: dict = {"name": template, "language": {"code": lang}}
    if params:
        tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
    r = http("POST", _url(f"{phone_number_id}/messages"), headers={"Authorization": f"Bearer {token}"}, json={
        "messaging_product": "whatsapp", "to": to, "type": "template", "template": tpl,
    })
    return {"id": (r.get("messages") or [{}])[0].get("id")}


def list_templates(waba_id: str, token: str) -> list[dict]:
    r = http("GET", _url(f"{waba_id}/message_templates"), params={"fields": "name,status,language,category", "limit": 100, "access_token": token})
    return r.get("data", [])


def parse_inbound(payload: dict) -> list[dict]:
    """Extract inbound text messages from a webhook payload."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            pnid = value.get("metadata", {}).get("phone_number_id")
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                if m.get("type") == "text":
                    out.append({"phone_number_id": pnid, "from": m.get("from"), "name": names.get(m.get("from"), ""), "text": m["text"]["body"], "id": m.get("id")})
    return out
