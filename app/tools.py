"""Tools the personas use to do real work, plus the approval actions that
run when the client approves something.

Rule: tools may freely create drafts inside SAHM (posts, sites, automations,
projects). Anything that leaves SAHM or goes live (publishing, replying,
messaging customers, activating automations, publishing a site) becomes an
approval the client must accept.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app import billing, config, db, media
from app.channels import ChannelError, PLATFORM_LABELS, meta as meta_api, n8n as n8n_api, whatsapp
from app.services import automations, channels as ch, posts, projects, sites


class ToolError(Exception):
    pass


def local_now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.settings.timezone))


def parse_local(value: str | None) -> int | None:
    """'YYYY-MM-DD HH:MM' in the client's timezone → epoch seconds."""
    if not value:
        return None
    value = str(value).strip().replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            d = dt.datetime.strptime(value[:n], fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            d = d.replace(hour=10)
        return int(d.replace(tzinfo=ZoneInfo(config.settings.timezone)).timestamp())
    raise ToolError(f"invalid datetime '{value}', use YYYY-MM-DD HH:MM")


def fmt_local(ts: int | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts, ZoneInfo(config.settings.timezone)).strftime("%Y-%m-%d %H:%M")


# ---- approvals --------------------------------------------------------------

def request_approval(user_id: str, persona: str, action: str, title: str, payload: dict) -> dict:
    row = {"id": db.new_id(), "user_id": user_id, "persona": persona, "action": action, "title": title[:300],
           "payload": db.jdump(payload), "status": "pending", "result": None, "created_at": db.now(), "decided_at": None}
    db.insert("approvals", row)
    db.notify(user_id, "طلب موافقة جديد", title, "#/approvals")
    return {"approval_id": row["id"], "status": "pending_client_approval", "note": "تمت إضافته إلى «بانتظار موافقتك»"}


def execute_approval(user_id: str, approval_id: str) -> dict:
    ap = db.one("SELECT * FROM approvals WHERE id = ? AND user_id = ?", (approval_id, user_id))
    if not ap:
        raise ToolError("approval_not_found")
    if ap["status"] != "pending":
        raise ToolError("already_decided")
    claimed = db.run("UPDATE approvals SET status = 'executing', decided_at = ? WHERE id = ? AND status = 'pending'", (db.now(), approval_id))
    if not claimed:
        raise ToolError("already_decided")
    payload = db.jload(ap["payload"], {})
    try:
        result = APPROVAL_ACTIONS[ap["action"]](user_id, payload)
        status = "approved"
    except (ToolError, ChannelError, ValueError, billing.LimitReached, automations.AutomationError) as exc:
        result = {"error": _err(exc)}
        status = "failed"
    db.update("approvals", {"id": approval_id}, {"status": status, "result": db.jdump(result)})
    db.log_activity(user_id, "approval", f"{'اعتماد' if status == 'approved' else 'تعذّر تنفيذ'}: {ap['title']}", approval_id)
    return {"status": status, "result": result}


def reject_approval(user_id: str, approval_id: str) -> None:
    ap = db.one("SELECT * FROM approvals WHERE id = ? AND user_id = ? AND status = 'pending'", (approval_id, user_id))
    if not ap:
        raise ToolError("approval_not_found")
    db.update("approvals", {"id": approval_id}, {"status": "rejected", "decided_at": db.now()})
    payload = db.jload(ap["payload"], {})
    if ap["action"] == "publish_post":
        db.run("UPDATE posts SET status = 'draft', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'pending_approval'", (db.now(), payload.get("post_id"), user_id))


def _err(exc: Exception) -> str:
    if isinstance(exc, billing.LimitReached):
        return f"وصلت لحد «{billing.METRIC_LABELS_AR.get(exc.metric, exc.metric)}» في باقتك ({exc.limit}). يمكنك الترقية من صفحة الاشتراك."
    return str(exc)[:400]


def _ap_publish_post(user_id: str, p: dict) -> dict:
    post = posts.approve(user_id, p["post_id"])
    if post["scheduled_at"] <= db.now() + 60:
        results = posts.publish(post)
        return {"post_id": post["id"], "published": True, "results": results}
    return {"post_id": post["id"], "scheduled_for": fmt_local(post["scheduled_at"])}


def _ap_reply_comment(user_id: str, p: dict) -> dict:
    c = ch.get(user_id, p["channel_id"])
    if not c:
        raise ToolError("channel_not_found")
    return meta_api.reply_comment(c["platform"], p["comment_id"], ch.secret(c), p["message"])


def _ap_whatsapp_campaign(user_id: str, p: dict) -> dict:
    c = ch.get(user_id, p["channel_id"])
    if not c or c["platform"] != "whatsapp":
        raise ToolError("whatsapp_channel_not_found")
    q = "SELECT * FROM contacts WHERE user_id = ? AND opted_in = 1 AND phone IS NOT NULL AND phone != ''"
    params: list = [user_id]
    if p.get("tag"):
        q += " AND (',' || tags || ',') LIKE ?"
        params.append(f"%,{p['tag']},%")
    recipients = db.all_(q, params)
    billing.check(user_id, "whatsapp_sends", len(recipients))
    sent, failed = 0, []
    for r in recipients:
        try:
            whatsapp.send_template(c["external_id"], ch.secret(c), r["phone"], p["template"], p.get("lang", "ar"),
                                   [automations.render(x, {"contact": r}) for x in p.get("params", [])])
            sent += 1
        except ChannelError as exc:
            failed.append({"phone": r["phone"][-4:], "error": str(exc)[:120]})
    billing.consume(user_id, "whatsapp_sends", sent)
    return {"sent": sent, "failed": len(failed), "failures": failed[:10]}


def _ap_publish_site(user_id: str, p: dict) -> dict:
    s = sites.publish(user_id, p["site_id"])
    return {"site_id": s["id"], "url": sites.public_url(s)}


def _ap_activate_automation(user_id: str, p: dict) -> dict:
    a = automations.activate(user_id, p["automation_id"])
    return {"automation_id": a["id"], "status": a["status"], "next_run": fmt_local(a["next_run_at"])}


def _ap_push_n8n(user_id: str, p: dict) -> dict:
    a = automations.get(user_id, p["automation_id"])
    c = ch.get(user_id, p["channel_id"])
    if not a or not c or c["platform"] != "n8n":
        raise ToolError("not_found")
    return n8n_api.create_workflow(ch.meta(c)["base_url"], ch.secret(c), automations.to_n8n(a))


APPROVAL_ACTIONS: dict[str, Callable[[str, dict], dict]] = {
    "publish_post": _ap_publish_post,
    "reply_comment": _ap_reply_comment,
    "whatsapp_campaign": _ap_whatsapp_campaign,
    "publish_site": _ap_publish_site,
    "activate_automation": _ap_activate_automation,
    "push_n8n": _ap_push_n8n,
}


# ---- tool definitions ----------------------------------------------------------

def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
TASKS_SCHEMA = {"type": "array", "items": _schema({
    "title": S, "details": S, "assignee": {"type": "string", "enum": list(projects.ASSIGNEES)},
    "due_date": {"type": "string", "description": "YYYY-MM-DD"}, "priority": {"type": "string", "enum": list(projects.PRIORITIES)},
}, ["title"])}

COMMON_TOOLS = [
    {"name": "get_business_profile", "description": "Read the client's business profile (name, industry, products, audience, tone, city, contact, brand color, FAQ).", "input_schema": _schema({})},
    {"name": "save_business_profile", "description": "Save details the client told you about their business into the shared profile used by the whole team. Only fields the client stated.", "input_schema": _schema({k: S for k in ("business_name", "industry", "description", "audience", "products", "tone", "city", "contact", "brand_color", "faq")})},
    {"name": "handoff_to_teammate", "description": "Create a task for a teammate (sulaf=marketing, najd=projects, sari=websites/code, sanad=automation) inside the client's inbox project.", "input_schema": _schema({"assignee": {"type": "string", "enum": ["sulaf", "najd", "sari", "sanad"]}, "title": S, "details": S}, ["assignee", "title"])},
]

PERSONA_TOOLS: dict[str, list[dict]] = {
    "sulaf": [
        {"name": "list_channels", "description": "List the client's connected accounts (Instagram, Facebook, TikTok, Telegram, WhatsApp) with their ids.", "input_schema": _schema({})},
        {"name": "design_post_image", "description": "Render a branded 4:5 post image (JPEG) and return its media_id. style: bold | minimal | quote | offer.", "input_schema": _schema({"headline": S, "subline": S, "footer": S, "style": {"type": "string", "enum": ["bold", "minimal", "quote", "offer"]}}, ["headline"])},
        {"name": "create_post", "description": "Create a post on chosen channels with optional media and schedule ('YYYY-MM-DD HH:MM' client local time; omit = publish right after approval). Sends it to the client for approval.", "input_schema": _schema({"caption": S, "channel_ids": {"type": "array", "items": S}, "media_ids": {"type": "array", "items": S}, "scheduled_at": S}, ["caption", "channel_ids"])},
        {"name": "list_posts", "description": "List recent posts with status, schedule and results.", "input_schema": _schema({"status": {"type": "string", "enum": ["draft", "pending_approval", "scheduled", "published", "partial", "failed"]}})},
        {"name": "get_post_insights", "description": "Fetch fresh performance numbers (likes, comments, reach, saves, shares) for a published post.", "input_schema": _schema({"post_id": S}, ["post_id"])},
        {"name": "list_recent_comments", "description": "Read recent comments on the latest published posts of a Facebook/Instagram channel.", "input_schema": _schema({"channel_id": S, "posts": {"type": "integer"}}, ["channel_id"])},
        {"name": "reply_to_comment", "description": "Propose a reply to a comment; it is sent after the client approves.", "input_schema": _schema({"channel_id": S, "comment_id": S, "comment_text": S, "message": S}, ["channel_id", "comment_id", "message"])},
        {"name": "contacts_summary", "description": "Count the client's contacts, opted-in WhatsApp contacts and tags.", "input_schema": _schema({})},
        {"name": "list_whatsapp_templates", "description": "List approved WhatsApp message templates for a WhatsApp channel.", "input_schema": _schema({"channel_id": S}, ["channel_id"])},
        {"name": "whatsapp_campaign", "description": "Prepare a WhatsApp template campaign to opted-in contacts (optionally filtered by tag). Sent after client approval.", "input_schema": _schema({"channel_id": S, "template": S, "lang": S, "params": {"type": "array", "items": S}, "tag": S}, ["channel_id", "template"])},
    ],
    "najd": [
        {"name": "create_project_plan", "description": "Create a project with its tasks (phased, dated, prioritized, assigned to sulaf/najd/sari/sanad/me).", "input_schema": _schema({"name": S, "description": S, "goal": S, "tasks": TASKS_SCHEMA}, ["name", "tasks"])},
        {"name": "list_projects", "description": "List projects with task counts by status.", "input_schema": _schema({})},
        {"name": "get_project", "description": "Get a project's tasks with ids, status, due dates and assignees.", "input_schema": _schema({"project_id": S}, ["project_id"])},
        {"name": "add_tasks", "description": "Add tasks to an existing project.", "input_schema": _schema({"project_id": S, "tasks": TASKS_SCHEMA}, ["project_id", "tasks"])},
        {"name": "update_task", "description": "Update a task's status (new, planning, in_progress, review, done, blocked), due date, assignee, priority or details.", "input_schema": _schema({"task_id": S, "status": S, "due_date": S, "assignee": S, "priority": S, "details": S}, ["task_id"])},
        {"name": "send_progress_report", "description": "Email the client a progress report of all projects now.", "input_schema": _schema({})},
        {"name": "schedule_weekly_report", "description": "Set up a weekly progress report email (weekday 0=Sunday..6, hour 0-23, client time). Activated after client approval.", "input_schema": _schema({"weekday": {"type": "integer"}, "hour": {"type": "integer"}}, ["weekday", "hour"])},
        {"name": "calendar_link", "description": "Get the client's calendar (ICS) download link for dated tasks.", "input_schema": _schema({"project_id": S})},
    ],
    "sari": [
        {"name": "create_site", "description": "Create a website/landing page from a complete self-contained HTML document. Returns site id, preview link and form endpoint.", "input_schema": _schema({"title": S, "html": S}, ["title", "html"])},
        {"name": "update_site", "description": "Replace a site's HTML with a revised full document (keeps version history count).", "input_schema": _schema({"site_id": S, "html": S, "title": S}, ["site_id", "html"])},
        {"name": "get_site", "description": "Get a site's current HTML to revise it.", "input_schema": _schema({"site_id": S}, ["site_id"])},
        {"name": "list_sites", "description": "List the client's sites with status and links.", "input_schema": _schema({})},
        {"name": "request_publish_site", "description": "Ask the client to approve publishing a site on its public link.", "input_schema": _schema({"site_id": S}, ["site_id"])},
    ],
    "sanad": [
        {"name": "list_channels", "description": "List connected channels (WhatsApp, Telegram, n8n, social) with ids.", "input_schema": _schema({})},
        {"name": "create_automation", "description": (
            "Create an automation draft and ask the client to activate it. trigger.type: schedule {cron, 5 fields, client time} | webhook | manual | "
            "whatsapp_inbound {channel_id} | telegram_inbound {channel_id} | site_form. steps[].type: ai_reply {instructions} (writes {{output}} from the business profile) | "
            "ai_generate {prompt} | reply {text} (answers the inbound sender) | send_email {to, subject, body} | telegram_message {channel_id, text} | "
            "whatsapp_message {channel_id, to, text} | whatsapp_template {channel_id, to, template, lang, params} | http_request {method, url, body} | "
            "create_task {title, details, assignee} | notify {title, body} | project_report {}. Placeholders: {{output}}, {{now}}, {{trigger.text}}, "
            "{{trigger.from}}, {{trigger.name}}, {{trigger.form.name}}, {{trigger.form.phone}}, {{trigger.body}}."),
            "input_schema": _schema({"name": S, "description": S, "trigger": {"type": "object"}, "steps": {"type": "array", "items": {"type": "object"}}}, ["name", "trigger", "steps"])},
        {"name": "list_automations", "description": "List automations with status, trigger, next run and webhook URL.", "input_schema": _schema({})},
        {"name": "automation_runs", "description": "Read the latest run logs of an automation to diagnose problems.", "input_schema": _schema({"automation_id": S}, ["automation_id"])},
        {"name": "run_automation_now", "description": "Run an ACTIVE automation once now (manual test).", "input_schema": _schema({"automation_id": S}, ["automation_id"])},
        {"name": "push_to_n8n", "description": "Export an automation to the client's connected n8n instance (after approval).", "input_schema": _schema({"automation_id": S, "channel_id": S}, ["automation_id", "channel_id"])},
    ],
}


def tools_for(persona: str) -> list[dict]:
    return PERSONA_TOOLS.get(persona, []) + COMMON_TOOLS


# ---- executors -------------------------------------------------------------------

def run_tool(user: dict, persona: str, name: str, args: dict) -> Any:
    allowed = {t["name"] for t in tools_for(persona)}
    if name not in allowed:
        raise ToolError(f"tool {name} is not available to {persona}")
    fn = EXECUTORS[name]
    try:
        return fn(user, persona, args or {})
    except billing.LimitReached as exc:
        raise ToolError(_err(exc)) from exc
    except (ChannelError, ValueError, automations.AutomationError, media.MediaError) as exc:
        raise ToolError(_err(exc)) from exc


def _profile(user, persona, a):
    p = db.one("SELECT * FROM business_profiles WHERE user_id = ?", (user["id"],))
    return {k: v for k, v in (p or {}).items() if k not in ("user_id", "updated_at")} or {"empty": True, "hint": "اطلب من العميل تعبئة ملف النشاط أو احفظ ما يذكره"}


def _save_profile(user, persona, a):
    fields = {k: str(v)[:3000] for k, v in a.items() if k in ("business_name", "industry", "description", "audience", "products", "tone", "city", "contact", "brand_color", "faq") and v}
    if not fields:
        return {"saved": False}
    existing = db.one("SELECT user_id FROM business_profiles WHERE user_id = ?", (user["id"],))
    if existing:
        db.update("business_profiles", {"user_id": user["id"]}, {**fields, "updated_at": db.now()})
    else:
        db.insert("business_profiles", {"user_id": user["id"], **fields, "updated_at": db.now()})
    return {"saved": True, "fields": list(fields)}


def _handoff(user, persona, a):
    inbox = projects.inbox(user["id"])
    t = projects.add_task(user["id"], inbox["id"], a["title"], a.get("details", ""), a["assignee"])
    return {"task_id": t["id"], "project": inbox["name"], "assignee": a["assignee"]}


def _list_channels(user, persona, a):
    return [{"id": c["id"], "platform": c["platform"], "platform_ar": PLATFORM_LABELS.get(c["platform"]), "name": c["name"], "status": c["status"], "last_error": c["last_error"]} for c in ch.list_for(user["id"])] or {"none": True, "hint": "لا توجد حسابات مربوطة؛ الربط من صفحة «الحسابات»"}


def _design(user, persona, a):
    prof = db.one("SELECT business_name, brand_color FROM business_profiles WHERE user_id = ?", (user["id"],)) or {}
    row = media.design_post(user["id"], a["headline"], a.get("subline", ""), a.get("footer") or prof.get("business_name", ""), a.get("style", "bold"), prof.get("brand_color") or "#6d3df5")
    return {"media_id": row["id"], "preview_url": media.public_url(row)}


def _create_post(user, persona, a):
    when = parse_local(a.get("scheduled_at"))
    if when and when < db.now() - 300:
        raise ToolError("scheduled time is in the past")
    chans = [ch.get(user["id"], c) for c in a.get("channel_ids", [])]
    chans = [c for c in chans if c]
    if not chans:
        raise ToolError("no valid channel_ids — call list_channels first")
    bad = [c["name"] for c in chans if c["platform"] not in posts.PUBLISHABLE]
    if bad:
        raise ToolError(f"these channels cannot publish posts: {bad}")
    if any(c["platform"] in ("instagram", "tiktok") for c in chans) and not a.get("media_ids"):
        raise ToolError("Instagram/TikTok need an image or video: call design_post_image first or ask the client to upload")
    p = posts.create(user["id"], a["caption"], a.get("media_ids", []), [c["id"] for c in chans], when)
    db.update("posts", {"id": p["id"]}, {"status": "pending_approval"})
    names = "، ".join(f"{PLATFORM_LABELS.get(c['platform'])} ({c['name']})" for c in chans)
    ap = request_approval(user["id"], persona, "publish_post", f"نشر منشور على {names}" + (f" في {fmt_local(when)}" if when else " فور الاعتماد"), {"post_id": p["id"]})
    return {"post_id": p["id"], **ap}


def _list_posts(user, persona, a):
    q, params = "SELECT * FROM posts WHERE user_id = ?", [user["id"]]
    if a.get("status"):
        q += " AND status = ?"
        params.append(a["status"])
    rows = db.all_(q + " ORDER BY created_at DESC LIMIT 20", params)
    return [{"id": r["id"], "caption": r["caption"][:140], "status": r["status"], "scheduled_at": fmt_local(r["scheduled_at"]), "published_at": fmt_local(r["published_at"]), "results": db.jload(r["results"], {})} for r in rows]


def _insights(user, persona, a):
    p = posts.get(user["id"], a["post_id"])
    if not p:
        raise ToolError("post_not_found")
    if p["status"] not in ("published", "partial"):
        raise ToolError("post is not published yet")
    return posts.refresh_insights(p)


def _comments(user, persona, a):
    c = ch.get(user["id"], a["channel_id"])
    if not c or c["platform"] not in ("facebook", "instagram"):
        raise ToolError("comments are available for Facebook/Instagram channels")
    token = ch.secret(c)
    recent = db.all_("SELECT * FROM posts WHERE user_id = ? AND status IN ('published','partial') ORDER BY published_at DESC LIMIT ?", (user["id"], int(a.get("posts") or 5)))
    out = []
    for p in recent:
        res = db.jload(p["results"], {}).get(c["id"])
        if res and res.get("ok") and res.get("id"):
            for cm in meta_api.list_comments(c["platform"], res["id"], token):
                out.append({"post": p["caption"][:60], **cm})
    return out or {"none": True}


def _reply_comment(user, persona, a):
    c = ch.get(user["id"], a["channel_id"])
    if not c:
        raise ToolError("channel_not_found")
    return request_approval(user["id"], persona, "reply_comment", f"رد على تعليق «{(a.get('comment_text') or '')[:60]}»: {a['message'][:80]}", {"channel_id": c["id"], "comment_id": a["comment_id"], "message": a["message"]})


def _contacts(user, persona, a):
    total = db.one("SELECT COUNT(*) n FROM contacts WHERE user_id = ?", (user["id"],))["n"]
    opted = db.one("SELECT COUNT(*) n FROM contacts WHERE user_id = ? AND opted_in = 1 AND phone != ''", (user["id"],))["n"]
    tags: dict[str, int] = {}
    for r in db.all_("SELECT tags FROM contacts WHERE user_id = ?", (user["id"],)):
        for t in [x.strip() for x in r["tags"].split(",") if x.strip()]:
            tags[t] = tags.get(t, 0) + 1
    return {"total": total, "whatsapp_opted_in": opted, "tags": tags}


def _wa_templates(user, persona, a):
    c = ch.get(user["id"], a["channel_id"])
    if not c or c["platform"] != "whatsapp":
        raise ToolError("whatsapp_channel_not_found")
    waba = ch.meta(c).get("waba_id")
    if not waba:
        raise ToolError("WhatsApp Business Account ID is missing on this channel")
    return whatsapp.list_templates(waba, ch.secret(c))


def _wa_campaign(user, persona, a):
    c = ch.get(user["id"], a["channel_id"])
    if not c or c["platform"] != "whatsapp":
        raise ToolError("whatsapp_channel_not_found")
    n = _contacts(user, persona, {})["whatsapp_opted_in"]
    return request_approval(user["id"], persona, "whatsapp_campaign", f"حملة واتساب بقالب «{a['template']}» إلى المشتركين" + (f" (وسم {a['tag']})" if a.get("tag") else "") + f" — حتى {n} رقم",
                            {"channel_id": c["id"], "template": a["template"], "lang": a.get("lang", "ar"), "params": a.get("params", []), "tag": a.get("tag")})


def _plan(user, persona, a):
    p = projects.create_project(user["id"], a["name"], a.get("description", ""), a.get("goal", ""))
    created = [projects.add_task(user["id"], p["id"], t.get("title", ""), t.get("details", ""), t.get("assignee", "najd"), t.get("due_date"), t.get("priority", "medium"))["id"] for t in a.get("tasks", [])[:60]]
    return {"project_id": p["id"], "tasks_created": len(created), "link": f"#/projects/{p['id']}"}


def _list_projects(user, persona, a):
    return [{"id": p["id"], "name": p["name"], "goal": p["goal"], "counts": p["counts"], "total": p["total"]} for p in projects.list_projects(user["id"])]


def _get_project(user, persona, a):
    p = projects.get(user["id"], a["project_id"])
    if not p:
        raise ToolError("project_not_found")
    return {"project": {"id": p["id"], "name": p["name"], "goal": p["goal"]}, "today": local_now().strftime("%Y-%m-%d"),
            "tasks": [{k: t[k] for k in ("id", "title", "status", "assignee", "due_date", "priority")} | {"output": t["output"][:200]} for t in projects.tasks_for(user["id"], p["id"])]}


def _add_tasks(user, persona, a):
    ids = [projects.add_task(user["id"], a["project_id"], t.get("title", ""), t.get("details", ""), t.get("assignee", "najd"), t.get("due_date"), t.get("priority", "medium"))["id"] for t in a.get("tasks", [])[:60]]
    return {"added": len(ids)}


def _update_task(user, persona, a):
    t = projects.update_task(user["id"], a["task_id"], **{k: v for k, v in a.items() if k != "task_id"})
    return {k: t[k] for k in ("id", "title", "status", "assignee", "due_date", "priority")}


def _send_report(user, persona, a):
    from app import mailer
    text = projects.report(user["id"])
    if not text:
        return {"sent": False, "reason": "no tasks yet"}
    if not mailer.configured():
        return {"sent": False, "reason": "email is not configured on this server", "report": text}
    mailer.send(user["email"], "تقرير تقدّم مشاريعك — نجد | شهم", f"هلا {user['name']}،\n\nهذا ملخص مشاريعك:\n\n{text}\n\nنجد — فريق شهم")
    return {"sent": True, "to": user["email"]}


def _weekly_report(user, persona, a):
    wd, hr = int(a["weekday"]) % 7, max(0, min(23, int(a["hour"])))
    auto = automations.create(user["id"], "تقرير المشاريع الأسبوعي", "تقرير نجد الأسبوعي على الإيميل", {"type": "schedule", "cron": f"0 {hr} * * {wd}"},
                              [{"type": "project_report"}])
    days = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]
    return request_approval(user["id"], persona, "activate_automation", f"تفعيل تقرير أسبوعي كل {days[wd]} الساعة {hr}:00", {"automation_id": auto["id"]})


def _calendar(user, persona, a):
    from app.routes_util import calendar_url
    return {"url": calendar_url(user, a.get("project_id"))}


def _create_site(user, persona, a):
    s = sites.create(user["id"], a["title"], a["html"])
    if "__FORM__" in s["html"]:
        s = sites.update(user["id"], s["id"], s["html"].replace("__FORM__", f"{config.settings.base_url}/f/{s['slug']}"))
    return {"site_id": s["id"], "preview": f"#/sites/{s['id']}", "form_endpoint": f"{config.settings.base_url}/f/{s['slug']}", "public_url_after_publish": sites.public_url(s)}


def _update_site(user, persona, a):
    html = a["html"]
    s0 = sites.get(user["id"], a["site_id"])
    if not s0:
        raise ToolError("site_not_found")
    html = html.replace("__FORM__", f"{config.settings.base_url}/f/{s0['slug']}")
    s = sites.update(user["id"], a["site_id"], html, a.get("title"))
    note = "التعديل محفوظ كمسودة؛ اطلب إعادة النشر ليظهر للزوار" if s["status"] == "published" else "محفوظ كمسودة"
    return {"site_id": s["id"], "version": s["version"], "note": note}


def _get_site(user, persona, a):
    s = sites.get(user["id"], a["site_id"])
    if not s:
        raise ToolError("site_not_found")
    return {"title": s["title"], "status": s["status"], "html": s["html"][:120_000]}


def _list_sites(user, persona, a):
    return [sites.to_public(s) for s in db.all_("SELECT * FROM sites WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],))]


def _publish_site(user, persona, a):
    s = sites.get(user["id"], a["site_id"])
    if not s:
        raise ToolError("site_not_found")
    return request_approval(user["id"], persona, "publish_site", f"نشر موقع «{s['title']}» على {sites.public_url(s)}", {"site_id": s["id"]})


def _create_automation(user, persona, a):
    trig, steps = a.get("trigger") or {}, a.get("steps") or []
    auto = automations.create(user["id"], a["name"], a.get("description", ""), trig, steps)
    ap = request_approval(user["id"], persona, "activate_automation", f"تفعيل أتمتة «{a['name']}»", {"automation_id": auto["id"]})
    return {"automation_id": auto["id"], **automations.to_public(auto), **ap}


def _list_automations(user, persona, a):
    rows = db.all_("SELECT * FROM automations WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],))
    return [automations.to_public(r) | {"next_run_at": fmt_local(r["next_run_at"])} for r in rows]


def _runs(user, persona, a):
    if not automations.get(user["id"], a["automation_id"]):
        raise ToolError("automation_not_found")
    rows = db.all_("SELECT * FROM automation_runs WHERE automation_id = ? AND user_id = ? ORDER BY started_at DESC LIMIT 10", (a["automation_id"], user["id"]))
    return [{"at": fmt_local(r["started_at"]), "trigger": r["trigger_kind"], "status": r["status"], "log": db.jload(r["log"], [])} for r in rows] or {"none": True}


def _run_now(user, persona, a):
    auto = automations.get(user["id"], a["automation_id"])
    if not auto:
        raise ToolError("automation_not_found")
    if auto["status"] != "active":
        raise ToolError("the automation must be activated by the client first")
    return automations.run(auto, "manual", {"text": "تجربة يدوية", "from": "", "name": user["name"]})


def _push_n8n(user, persona, a):
    auto = automations.get(user["id"], a["automation_id"])
    c = ch.get(user["id"], a["channel_id"])
    if not auto or not c or c["platform"] != "n8n":
        raise ToolError("automation or n8n connection not found")
    return request_approval(user["id"], persona, "push_n8n", f"إرسال أتمتة «{auto['name']}» إلى n8n", {"automation_id": auto["id"], "channel_id": c["id"]})


EXECUTORS: dict[str, Callable] = {
    "get_business_profile": _profile, "save_business_profile": _save_profile, "handoff_to_teammate": _handoff,
    "list_channels": _list_channels, "design_post_image": _design, "create_post": _create_post, "list_posts": _list_posts,
    "get_post_insights": _insights, "list_recent_comments": _comments, "reply_to_comment": _reply_comment,
    "contacts_summary": _contacts, "list_whatsapp_templates": _wa_templates, "whatsapp_campaign": _wa_campaign,
    "create_project_plan": _plan, "list_projects": _list_projects, "get_project": _get_project, "add_tasks": _add_tasks,
    "update_task": _update_task, "send_progress_report": _send_report, "schedule_weekly_report": _weekly_report, "calendar_link": _calendar,
    "create_site": _create_site, "update_site": _update_site, "get_site": _get_site, "list_sites": _list_sites, "request_publish_site": _publish_site,
    "create_automation": _create_automation, "list_automations": _list_automations, "automation_runs": _runs,
    "run_automation_now": _run_now, "push_to_n8n": _push_n8n,
}
