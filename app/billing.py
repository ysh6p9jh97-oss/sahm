"""Plans, usage limits and Moyasar payments.

Payment model: one Moyasar invoice per billing period. When Moyasar reports
the invoice's payment as paid (webhook, double-checked against the Moyasar API)
the subscription is extended by 30 days. Prices live here, in one place.
"""
from __future__ import annotations

import datetime as dt
import hmac
from typing import Any

import requests

from app import config, db

MOYASAR_API = "https://api.moyasar.com/v1"
PERIOD_DAYS = 30

# Limits are per calendar month. None = unlimited.
PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name_ar": "المجانية", "name_en": "Free", "price_sar": 0,
        "limits": {"messages": 40, "posts": 4, "channels": 1, "sites_published": 1, "automations_active": 1, "automation_runs": 100, "whatsapp_sends": 0, "projects": 2},
    },
    "pro": {
        "name_ar": "الاحترافية", "name_en": "Pro", "price_sar": 149,
        "limits": {"messages": 1000, "posts": 90, "channels": 6, "sites_published": 5, "automations_active": 10, "automation_runs": 5000, "whatsapp_sends": 1000, "projects": 25},
    },
    "business": {
        "name_ar": "الأعمال", "name_en": "Business", "price_sar": 399,
        "limits": {"messages": 4000, "posts": 400, "channels": 20, "sites_published": 20, "automations_active": 50, "automation_runs": 50000, "whatsapp_sends": 10000, "projects": None},
    },
}

METRIC_LABELS_AR = {
    "messages": "الرسائل مع الفريق",
    "posts": "المنشورات المنشورة",
    "channels": "الحسابات المربوطة",
    "sites_published": "المواقع المنشورة",
    "automations_active": "الأتمتات المفعّلة",
    "automation_runs": "تشغيلات الأتمتة",
    "whatsapp_sends": "رسائل واتساب",
    "projects": "المشاريع",
}


class LimitReached(Exception):
    def __init__(self, metric: str, limit: int):
        super().__init__(f"limit reached: {metric} ({limit})")
        self.metric = metric
        self.limit = limit


def period(ts: int | None = None) -> str:
    d = dt.datetime.fromtimestamp(ts or db.now(), dt.timezone.utc)
    return d.strftime("%Y-%m")


def subscription(user_id: str) -> dict:
    sub = db.one("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
    if not sub:
        sub = {"user_id": user_id, "plan": "free", "status": "active", "current_period_end": None, "updated_at": db.now()}
        db.insert("subscriptions", sub)
    if sub["plan"] != "free" and sub["current_period_end"] and sub["current_period_end"] < db.now():
        db.update("subscriptions", {"user_id": user_id}, {"plan": "free", "status": "expired", "updated_at": db.now()})
        sub = db.one("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
    return sub


def limits(user_id: str) -> dict:
    return PLANS[subscription(user_id)["plan"]]["limits"]


def used(user_id: str, metric: str) -> int:
    # Some metrics are current counts, not monthly counters.
    if metric == "channels":
        return db.one("SELECT COUNT(*) n FROM channels WHERE user_id = ?", (user_id,))["n"]
    if metric == "sites_published":
        return db.one("SELECT COUNT(*) n FROM sites WHERE user_id = ? AND status = 'published'", (user_id,))["n"]
    if metric == "automations_active":
        return db.one("SELECT COUNT(*) n FROM automations WHERE user_id = ? AND status = 'active'", (user_id,))["n"]
    if metric == "projects":
        return db.one("SELECT COUNT(*) n FROM projects WHERE user_id = ? AND status != 'archived'", (user_id,))["n"]
    row = db.one("SELECT amount FROM usage WHERE user_id = ? AND period = ? AND metric = ?", (user_id, period(), metric))
    return row["amount"] if row else 0


def check(user_id: str, metric: str, add: int = 1) -> None:
    limit = limits(user_id).get(metric)
    if limit is None:
        return
    if used(user_id, metric) + add > limit:
        raise LimitReached(metric, limit)


def consume(user_id: str, metric: str, amount: int = 1) -> None:
    db.run(
        "INSERT INTO usage (user_id, period, metric, amount) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, period, metric) DO UPDATE SET amount = amount + excluded.amount",
        (user_id, period(), metric, amount),
    )


def usage_summary(user_id: str) -> list[dict]:
    lim = limits(user_id)
    return [
        {"metric": m, "label": METRIC_LABELS_AR[m], "used": used(user_id, m), "limit": lim.get(m)}
        for m in METRIC_LABELS_AR
    ]


# ---- Moyasar ---------------------------------------------------------------

class PaymentsNotConfigured(Exception):
    pass


def _auth() -> tuple[str, str]:
    key = config.settings.moyasar_secret_key
    if not key:
        raise PaymentsNotConfigured("MOYASAR_SECRET_KEY is not set")
    return (key, "")


def create_invoice(user: dict, plan: str) -> dict:
    if plan not in PLANS or plan == "free":
        raise ValueError("invalid plan")
    price = PLANS[plan]["price_sar"]
    invoice_id = db.new_id()
    base = config.settings.base_url
    body = {
        "amount": int(price * 100),
        "currency": "SAR",
        "description": f"SAHM | شهم — {PLANS[plan]['name_ar']} ({PERIOD_DAYS} يوم)",
        "success_url": f"{base}/app#/billing?paid=1",
        "back_url": f"{base}/app#/billing",
        "callback_url": f"{base}/api/billing/callback",
        "metadata": {"sahm_invoice": invoice_id, "user_id": user["id"], "plan": plan},
    }
    resp = requests.post(f"{MOYASAR_API}/invoices", json=body, auth=_auth(), timeout=20)
    if resp.status_code >= 300:
        raise RuntimeError(f"Moyasar error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    db.insert("invoices", {
        "id": invoice_id, "user_id": user["id"], "provider_id": data.get("id"), "plan": plan,
        "amount_halalas": body["amount"], "status": data.get("status", "initiated"),
        "url": data.get("url"), "created_at": db.now(), "paid_at": None,
    })
    return {"id": invoice_id, "url": data.get("url")}


def fetch_moyasar_invoice(provider_id: str) -> dict:
    resp = requests.get(f"{MOYASAR_API}/invoices/{provider_id}", auth=_auth(), timeout=20)
    resp.raise_for_status()
    return resp.json()


def verify_webhook_secret(received: str | None) -> bool:
    expected = config.settings.moyasar_webhook_secret
    return bool(expected) and bool(received) and hmac.compare_digest(str(received), expected)


def settle_invoice(provider_invoice_id: str) -> bool:
    """Confirm with Moyasar that the invoice is paid, then activate the plan.

    Idempotent: a paid invoice is applied once.
    """
    inv = db.one("SELECT * FROM invoices WHERE provider_id = ?", (provider_invoice_id,))
    if not inv:
        return False
    if inv["status"] == "paid":
        return True
    remote = fetch_moyasar_invoice(provider_invoice_id)
    if remote.get("status") != "paid" or int(remote.get("amount", 0)) != inv["amount_halalas"]:
        return False
    activate_plan(inv["user_id"], inv["plan"], invoice_id=inv["id"])
    return True


def activate_plan(user_id: str, plan: str, invoice_id: str | None = None) -> None:
    with db.transaction():
        if invoice_id:
            changed = db.run("UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ? AND status != 'paid'", (db.now(), invoice_id))
            if not changed:
                return
        sub = subscription(user_id)
        start = max(db.now(), sub["current_period_end"] or 0) if sub["plan"] == plan else db.now()
        db.update("subscriptions", {"user_id": user_id}, {
            "plan": plan, "status": "active", "current_period_end": start + PERIOD_DAYS * 86400, "updated_at": db.now(),
        })
    db.notify(user_id, "تم تفعيل اشتراكك", f"باقة {PLANS[plan]['name_ar']} مفعّلة لمدة {PERIOD_DAYS} يومًا.", "#/billing")
    db.log_activity(user_id, "billing", f"تفعيل باقة {PLANS[plan]['name_ar']}")
