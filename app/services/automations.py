"""Sanad's automation engine.

An automation = trigger + ordered steps. Triggers:
  schedule (cron, client timezone) · webhook (unique URL) · manual ·
  whatsapp_inbound · telegram_inbound · site_form
Steps run in order; each step can read variables with {{name}} placeholders.
Automations are created as drafts and run only after the client activates them.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import requests

from app import billing, config, db, llm, mailer
from app.channels import ChannelError, telegram, whatsapp
from app.security import UnsafeURL, check_public_url, limiter, random_token
from app.services import channels as ch

TRIGGERS = ("schedule", "webhook", "manual", "whatsapp_inbound", "telegram_inbound", "site_form")
STEP_TYPES = (
    "ai_reply", "ai_generate", "reply", "send_email", "telegram_message", "whatsapp_message",
    "whatsapp_template", "http_request", "create_task", "notify", "project_report",
)


class AutomationError(ValueError):
    pass


# ---- cron ------------------------------------------------------------------

def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b or step < 1:
            raise AutomationError(f"invalid cron field: {spec}")
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> tuple[set, set, set, set, set]:
    parts = expr.split()
    if len(parts) != 5:
        raise AutomationError("cron must have 5 fields: minute hour day month weekday")
    m, h, dom, mon, dow = parts
    dows = _field(dow.replace("7", "0") if dow != "*" else dow, 0, 6)
    return _field(m, 0, 59), _field(h, 0, 23), _field(dom, 1, 31), _field(mon, 1, 12), dows


def next_run(expr: str, after: int, tz: str | None = None) -> int:
    mins, hours, doms, mons, dows = parse_cron(expr)
    zone = ZoneInfo(tz or config.settings.timezone)
    t = dt.datetime.fromtimestamp(after, zone).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    dom_any = len(doms) == 31
    dow_any = len(dows) == 7
    for _ in range(366 * 24 * 60):
        cron_dow = (t.weekday() + 1) % 7  # Sunday = 0
        day_ok = (t.day in doms and cron_dow in dows) if (dom_any or dow_any) else (t.day in doms or cron_dow in dows)
        if t.month in mons and day_ok and t.hour in hours and t.minute in mins:
            return int(t.timestamp())
        t += dt.timedelta(minutes=1)
    raise AutomationError("cron never fires")


# ---- validation ------------------------------------------------------------

def validate(user_id: str, trigger: dict, steps: list[dict]) -> None:
    kind = trigger.get("type")
    if kind not in TRIGGERS:
        raise AutomationError(f"unknown trigger: {kind}")
    if kind == "schedule":
        parse_cron(str(trigger.get("cron", "")))
    if kind in ("whatsapp_inbound", "telegram_inbound"):
        platform = "whatsapp" if kind == "whatsapp_inbound" else "telegram"
        if not ch.get(user_id, str(trigger.get("channel_id", ""))) or ch.get(user_id, trigger["channel_id"])["platform"] != platform:
            raise AutomationError(f"trigger needs a connected {platform} channel_id")
    if not steps or len(steps) > 15:
        raise AutomationError("an automation needs 1–15 steps")
    for s in steps:
        t = s.get("type")
        if t not in STEP_TYPES:
            raise AutomationError(f"unknown step: {t}")
        if t in ("telegram_message", "whatsapp_message", "whatsapp_template"):
            c = ch.get(user_id, str(s.get("channel_id", "")))
            if not c or c["platform"] != t.split("_")[0]:
                raise AutomationError(f"step {t} needs a connected {t.split('_')[0]} channel_id")
        if t == "http_request":
            try:
                check_public_url(str(s.get("url", "")))
            except UnsafeURL as exc:
                raise AutomationError(f"http_request url rejected: {exc}") from exc
        if t == "reply" and kind not in ("whatsapp_inbound", "telegram_inbound"):
            raise AutomationError("reply step only works with whatsapp_inbound or telegram_inbound triggers")


def create(user_id: str, name: str, description: str, trigger: dict, steps: list[dict]) -> dict:
    validate(user_id, trigger, steps)
    row = {
        "id": db.new_id(), "user_id": user_id, "name": name[:120], "description": description[:1000],
        "trigger": db.jdump(trigger), "steps": db.jdump(steps), "status": "draft",
        "webhook_token": random_token(24), "next_run_at": None, "last_run_at": None,
        "created_at": db.now(), "updated_at": db.now(),
    }
    db.insert("automations", row)
    db.log_activity(user_id, "automation", f"إنشاء أتمتة: {name}", row["id"])
    return row


def get(user_id: str, auto_id: str) -> dict | None:
    return db.one("SELECT * FROM automations WHERE id = ? AND user_id = ?", (auto_id, user_id))


def to_public(row: dict) -> dict:
    trig = db.jload(row["trigger"], {})
    out = {
        "id": row["id"], "name": row["name"], "description": row["description"], "trigger": trig,
        "steps": db.jload(row["steps"], []), "status": row["status"], "next_run_at": row["next_run_at"],
        "last_run_at": row["last_run_at"], "created_at": row["created_at"],
    }
    if trig.get("type") == "webhook":
        out["webhook_url"] = f"{config.settings.base_url}/hooks/{row['webhook_token']}"
    return out


def activate(user_id: str, auto_id: str) -> dict:
    row = get(user_id, auto_id)
    if not row:
        raise AutomationError("not_found")
    if row["status"] != "active":
        billing.check(user_id, "automations_active")
    trig = db.jload(row["trigger"], {})
    validate(user_id, trig, db.jload(row["steps"], []))
    nxt = next_run(trig["cron"], db.now()) if trig["type"] == "schedule" else None
    db.update("automations", {"id": auto_id}, {"status": "active", "next_run_at": nxt, "updated_at": db.now()})
    if trig["type"] == "telegram_inbound":
        c = ch.get(user_id, trig["channel_id"])
        telegram.set_webhook(ch.secret(c), f"{config.settings.base_url}/hooks/telegram/{c['id']}", ch.meta(c)["webhook_secret"])
    db.log_activity(user_id, "automation", f"تفعيل أتمتة: {row['name']}", auto_id)
    return get(user_id, auto_id)


def pause(user_id: str, auto_id: str) -> None:
    db.run("UPDATE automations SET status = 'paused', next_run_at = NULL, updated_at = ? WHERE id = ? AND user_id = ?", (db.now(), auto_id, user_id))


# ---- execution ---------------------------------------------------------------

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def render(text: str, ctx: dict) -> str:
    def sub(m: re.Match) -> str:
        cur: object = ctx
        for part in m.group(1).split("."):
            cur = cur.get(part, "") if isinstance(cur, dict) else ""
        return str(cur)
    return _VAR.sub(sub, str(text or ""))


def business_context(user_id: str) -> str:
    p = db.one("SELECT * FROM business_profiles WHERE user_id = ?", (user_id,))
    if not p:
        return ""
    keys = [("business_name", "اسم النشاط"), ("industry", "المجال"), ("description", "الوصف"), ("products", "المنتجات والخدمات"),
            ("audience", "الجمهور"), ("city", "المدينة"), ("contact", "التواصل"), ("faq", "أسئلة شائعة وأجوبتها")]
    return "\n".join(f"{label}: {p[k]}" for k, label in keys if p.get(k))


def run(automation: dict, trigger_kind: str, payload: dict | None = None) -> dict:
    user_id = automation["user_id"]
    run_id = db.new_id()
    db.insert("automation_runs", {"id": run_id, "automation_id": automation["id"], "user_id": user_id, "trigger_kind": trigger_kind, "status": "running", "log": "[]", "started_at": db.now(), "finished_at": None})
    log: list[dict] = []
    ctx = {"trigger": payload or {}, "now": dt.datetime.now(ZoneInfo(config.settings.timezone)).strftime("%Y-%m-%d %H:%M"), "output": ""}
    status = "succeeded"
    try:
        billing.check(user_id, "automation_runs")
        billing.consume(user_id, "automation_runs")
        for i, step in enumerate(db.jload(automation["steps"], [])):
            try:
                result = _run_step(user_id, step, ctx)
                log.append({"step": i + 1, "type": step["type"], "ok": True, "result": str(result)[:500]})
            except Exception as exc:  # noqa: BLE001 — every failure is logged and stops the run
                log.append({"step": i + 1, "type": step.get("type"), "ok": False, "error": str(exc)[:500]})
                status = "failed"
                break
    except billing.LimitReached:
        status = "failed"
        log.append({"step": 0, "ok": False, "error": "limit_reached: automation_runs"})
    db.update("automation_runs", {"id": run_id}, {"status": status, "log": db.jdump(log), "finished_at": db.now()})
    db.update("automations", {"id": automation["id"]}, {"last_run_at": db.now()})
    if status == "failed":
        db.notify(user_id, f"تعثّرت أتمتة: {automation['name']}", (log[-1].get("error") if log else "") or "", "#/automations")
    return {"id": run_id, "status": status, "log": log}


def _run_step(user_id: str, step: dict, ctx: dict) -> str:
    t = step["type"]
    if t in ("ai_reply", "ai_generate"):
        instructions = render(step.get("instructions") or step.get("prompt", ""), ctx)
        biz = business_context(user_id)
        if t == "ai_reply":
            msg = ctx["trigger"].get("text", "")
            prompt = (
                "أنت موظف خدمة عملاء لهذا النشاط. اكتب ردًا قصيرًا ولطيفًا ودقيقًا على رسالة العميل، "
                "بنفس لغة العميل، واعتمد فقط على معلومات النشاط. إذا لم تعرف الجواب فقل إن الفريق سيتواصل معه، ولا تخترع أسعارًا أو وعودًا.\n\n"
                f"معلومات النشاط:\n{biz or '(لا توجد)'}\n\nتعليمات إضافية: {instructions or '—'}\n\nرسالة العميل:\n{msg}"
            )
        else:
            prompt = f"{instructions}\n\nمعلومات النشاط:\n{biz or '(لا توجد)'}"
        ctx["output"] = llm.complete(prompt, fast=True, max_tokens=800)
        return ctx["output"][:200]
    if t == "reply":
        text = render(step.get("text") or "{{output}}", ctx)
        src = ctx["trigger"]
        c = ch.get(user_id, src.get("channel_id", ""))
        if not c:
            raise AutomationError("source channel missing")
        if c["platform"] == "whatsapp":
            billing.check(user_id, "whatsapp_sends")
            r = whatsapp.send_text(c["external_id"], ch.secret(c), src["from"], text)
            billing.consume(user_id, "whatsapp_sends")
        else:
            r = telegram.send_message(ch.secret(c), src["chat_id"], text)
        return f"sent {r.get('id')}"
    if t == "send_email":
        to = render(step.get("to", ""), ctx).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            raise AutomationError("invalid recipient email")
        if not limiter.allow(f"email:{user_id}", 200, 86400):
            raise AutomationError("daily email limit reached")
        mailer.send(to, render(step.get("subject", "SAHM"), ctx), render(step.get("body", "{{output}}"), ctx))
        return f"email sent to {to}"
    if t == "telegram_message":
        c = ch.get(user_id, step["channel_id"])
        r = telegram.send_message(ch.secret(c), ch.meta(c)["chat_id"], render(step.get("text", "{{output}}"), ctx))
        return f"sent {r['id']}"
    if t in ("whatsapp_message", "whatsapp_template"):
        c = ch.get(user_id, step["channel_id"])
        to = re.sub(r"\D", "", render(step.get("to", ""), ctx))
        if not to:
            raise AutomationError("missing recipient phone")
        billing.check(user_id, "whatsapp_sends")
        if t == "whatsapp_template":
            params = [render(p, ctx) for p in step.get("params", [])]
            r = whatsapp.send_template(c["external_id"], ch.secret(c), to, step["template"], step.get("lang", "ar"), params)
        else:
            r = whatsapp.send_text(c["external_id"], ch.secret(c), to, render(step.get("text", "{{output}}"), ctx))
        billing.consume(user_id, "whatsapp_sends")
        return f"sent {r.get('id')}"
    if t == "http_request":
        url = check_public_url(render(step["url"], ctx))
        method = str(step.get("method", "POST")).upper()
        if method not in ("GET", "POST", "PUT", "PATCH"):
            raise AutomationError("unsupported method")
        body = step.get("body")
        if isinstance(body, dict):
            body = {k: render(v, ctx) if isinstance(v, str) else v for k, v in body.items()}
        resp = requests.request(method, url, json=body if method != "GET" else None, timeout=20, allow_redirects=False)
        ctx["http_status"] = resp.status_code
        ctx["http_body"] = resp.text[:2000]
        if resp.status_code >= 400:
            raise AutomationError(f"HTTP {resp.status_code}")
        return f"HTTP {resp.status_code}"
    if t == "create_task":
        from app.services import projects
        pid = step.get("project_id") or projects.inbox(user_id)["id"]
        if not projects.get(user_id, pid):
            pid = projects.inbox(user_id)["id"]
        task = projects.add_task(user_id, pid, render(step.get("title", "مهمة جديدة"), ctx), render(step.get("details", ""), ctx), step.get("assignee", "najd"), step.get("due_date"))
        return f"task {task['id']}"
    if t == "project_report":
        from app.services import projects
        user = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        text = projects.report(user_id)
        if not text:
            return "no tasks — nothing to send"
        mailer.send(user["email"], "تقريرك الأسبوعي — نجد | شهم", f"هلا {user['name']}،\n\nملخص مشاريعك هذا الأسبوع:\n\n{text}\n\nنجد — فريق شهم")
        return f"report sent to {user['email']}"
    if t == "notify":
        db.notify(user_id, render(step.get("title", "تنبيه"), ctx), render(step.get("body", ""), ctx), "#/automations")
        return "notified"
    raise AutomationError(f"unknown step {t}")


def run_due() -> int:
    """Run scheduled automations that are due (called by the worker)."""
    count = 0
    for a in db.all_("SELECT * FROM automations WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?", (db.now(),)):
        trig = db.jload(a["trigger"], {})
        nxt = next_run(trig["cron"], db.now())
        claimed = db.run("UPDATE automations SET next_run_at = ? WHERE id = ? AND next_run_at = ?", (nxt, a["id"], a["next_run_at"]))
        if claimed:
            run(a, "schedule")
            count += 1
    return count


def fire_event(user_id: str, trigger_type: str, payload: dict, channel_id: str | None = None) -> int:
    """Run every active automation of this user listening to an event."""
    n = 0
    for a in db.all_("SELECT * FROM automations WHERE user_id = ? AND status = 'active'", (user_id,)):
        trig = db.jload(a["trigger"], {})
        if trig.get("type") != trigger_type:
            continue
        if channel_id and trig.get("channel_id") not in (None, channel_id):
            continue
        run(a, trigger_type, payload)
        n += 1
    return n


def to_n8n(row: dict) -> dict:
    """Translate an automation into an n8n workflow (best-effort, for clients who run n8n)."""
    trig = db.jload(row["trigger"], {})
    nodes, connections = [], {}
    if trig.get("type") == "schedule":
        first = {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": trig["cron"]}]}}, "name": "Schedule", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0]}
    else:
        first = {"parameters": {"httpMethod": "POST", "path": row["webhook_token"][:16]}, "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0]}
    nodes.append(first)
    prev = first["name"]
    for i, s in enumerate(db.jload(row["steps"], []), start=1):
        name = f"{i}. {s['type']}"
        if s["type"] == "http_request":
            node = {"parameters": {"method": s.get("method", "POST"), "url": s["url"], "sendBody": True, "specifyBody": "json", "jsonBody": db.jdump(s.get("body", {}))}, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2}
        elif s["type"] == "send_email":
            node = {"parameters": {"toEmail": s.get("to", ""), "subject": s.get("subject", ""), "text": s.get("body", "")}, "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1}
        else:
            node = {"parameters": {"content": f"SAHM step: {s['type']}\n{db.jdump(s)}"}, "type": "n8n-nodes-base.stickyNote", "typeVersion": 1}
        node.update({"name": name, "position": [260 * i, 0]})
        nodes.append(node)
        if node["type"] != "n8n-nodes-base.stickyNote":
            connections[prev] = {"main": [[{"node": name, "type": "main", "index": 0}]]}
            prev = name
    return {"name": f"SAHM — {row['name']}", "nodes": nodes, "connections": connections, "settings": {"executionOrder": "v1"}}


def handle_inbound(channel: dict, trigger_type: str, payload: dict) -> int:
    try:
        return fire_event(channel["user_id"], trigger_type, {**payload, "channel_id": channel["id"]}, channel["id"])
    except ChannelError as exc:
        ch.mark_error(channel, str(exc))
        return 0
