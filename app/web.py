"""HTTP layer (Starlette): JSON API, OAuth callbacks, public webhooks, hosted
sites, media and the web app itself."""
from __future__ import annotations

import contextlib
import csv
import functools
import hashlib
import hmac
import io
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app import agent, attachments as attachment_service, billing, config, core, db, media, tools
from app.channels import ChannelError, meta as meta_api, n8n as n8n_api, telegram, tiktok, whatsapp
from app.personas import ORDER, PERSONAS
from app.routes_util import calendar_token, calendar_url
from app.security import (
    SESSION_COOKIE, SESSION_TTL, hash_password, issue_session, limiter, login_allowed, password_problem,
    random_token, read_session, record_failed_login, verify_password,
)
from app.services import automations, channels as ch, posts, projects, sites

log = logging.getLogger("sahm.web")
WEB = config.ROOT / "web"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class ApiError(Exception):
    def __init__(self, code: str, status: int = 400, message: str | None = None):
        super().__init__(code)
        self.code, self.status, self.message = code, status, message


def ok(data: Any = None, status: int = 200) -> JSONResponse:
    return JSONResponse(data if data is not None else {"ok": True}, status_code=status)


def api(auth: bool = True, max_body: int = 2_000_000):
    """Decorator: session auth, CSRF header on writes, JSON parsing, error mapping."""
    def wrap(fn: Callable):
        @functools.wraps(fn)
        async def handler(request: Request):
            try:
                user = None
                if auth:
                    user = await run_in_threadpool(read_session, request.cookies.get(SESSION_COOKIE))
                    if not user:
                        raise ApiError("unauthorized", 401)
                if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-sahm") != "1":
                    raise ApiError("csrf", 403)
                body: dict = {}
                if request.method in ("POST", "PUT", "PATCH", "DELETE") and "json" in request.headers.get("content-type", ""):
                    raw = await request.body()
                    if len(raw) > max_body:
                        raise ApiError("payload_too_large", 413)
                    body = json.loads(raw or b"{}")
                    if not isinstance(body, dict):
                        raise ApiError("invalid_json")
                request.state.user = user
                request.state.body = body
                return await fn(request)
            except ApiError as e:
                return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)
            except billing.LimitReached as e:
                return JSONResponse({"error": "limit_reached", "metric": e.metric, "limit": e.limit,
                                     "message": f"وصلت لحد «{billing.METRIC_LABELS_AR.get(e.metric, e.metric)}» في باقتك."}, status_code=402)
            except (tools.ToolError, automations.AutomationError, ChannelError, media.MediaError, ValueError) as e:
                return JSONResponse({"error": "bad_request", "message": str(e)[:400]}, status_code=400)
            except json.JSONDecodeError:
                return JSONResponse({"error": "invalid_json"}, status_code=400)
        return handler
    return wrap


def U(request: Request) -> dict:
    return request.state.user


def B(request: Request) -> dict:
    return request.state.body


def public_user(u: dict) -> dict:
    sub = billing.subscription(u["id"])
    return {"id": u["id"], "email": u["email"], "name": u["name"], "lang": u["lang"], "is_admin": bool(u["is_admin"]),
            "plan": sub["plan"], "plan_name": billing.PLANS[sub["plan"]]["name_ar"], "plan_ends": sub["current_period_end"]}


def set_session(resp: Response, user: dict) -> None:
    resp.set_cookie(SESSION_COOKIE, issue_session(user), max_age=SESSION_TTL, httponly=True, secure=config.settings.is_production,
                    samesite="lax", path="/")


# ---- auth -------------------------------------------------------------------------

@api(auth=False)
async def register(request: Request):
    b = B(request)
    name, email, pw = str(b.get("name", "")).strip()[:80], str(b.get("email", "")).strip().lower(), str(b.get("password", ""))
    if not name or not EMAIL_RE.match(email):
        raise ApiError("invalid_input", message="اكتب اسمك وبريدًا صحيحًا.")
    if password_problem(pw):
        raise ApiError("weak_password", message="كلمة المرور 8 أحرف على الأقل.")
    ip = request.client.host if request.client else "?"
    if not limiter.allow(f"register:{ip}", 10, 3600):
        raise ApiError("rate_limited", 429)
    if db.one("SELECT id FROM users WHERE email = ?", (email,)):
        raise ApiError("email_taken", 409, "هذا البريد مسجل مسبقًا.")
    user = {"id": db.new_id(), "email": email, "name": name, "password_hash": await run_in_threadpool(hash_password, pw), "lang": "ar",
            "is_admin": int(email in config.settings.admin_emails), "token_version": 0, "created_at": db.now()}
    db.insert("users", user)
    billing.subscription(user["id"])
    db.log_activity(user["id"], "account", "إنشاء الحساب")
    resp = ok(public_user(user), 201)
    set_session(resp, user)
    return resp


@api(auth=False)
async def login(request: Request):
    b = B(request)
    email, pw = str(b.get("email", "")).strip().lower(), str(b.get("password", ""))
    ip = request.client.host if request.client else "?"
    key = f"{email}|{ip}"
    if not login_allowed(key):
        raise ApiError("rate_limited", 429, "محاولات كثيرة، جرّب بعد ربع ساعة.")
    user = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if not user or not await run_in_threadpool(verify_password, pw, user["password_hash"]):
        record_failed_login(key)
        raise ApiError("invalid_credentials", 401, "البريد أو كلمة المرور غير صحيحة.")
    resp = ok(public_user(user))
    set_session(resp, user)
    return resp


@api()
async def logout(request: Request):
    resp = ok()
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@api()
async def me(request: Request):
    u = U(request)
    if request.method == "PATCH":
        b = B(request)
        vals = {}
        if b.get("name"):
            vals["name"] = str(b["name"]).strip()[:80]
        if b.get("lang") in ("ar", "en"):
            vals["lang"] = b["lang"]
        if vals:
            db.update("users", {"id": u["id"]}, vals)
        u = db.one("SELECT * FROM users WHERE id = ?", (u["id"],))
    return ok(public_user(u))


@api()
async def change_password(request: Request):
    u, b = U(request), B(request)
    if not verify_password(str(b.get("current", "")), u["password_hash"]):
        raise ApiError("invalid_credentials", 401, "كلمة المرور الحالية غير صحيحة.")
    if password_problem(str(b.get("new", ""))):
        raise ApiError("weak_password", message="كلمة المرور 8 أحرف على الأقل.")
    db.update("users", {"id": u["id"]}, {"password_hash": hash_password(b["new"]), "token_version": u["token_version"] + 1})
    u = db.one("SELECT * FROM users WHERE id = ?", (u["id"],))
    resp = ok()
    set_session(resp, u)
    return resp


@api()
async def delete_account(request: Request):
    u, b = U(request), B(request)
    if not verify_password(str(b.get("password", "")), u["password_hash"]):
        raise ApiError("invalid_credentials", 401, "كلمة المرور غير صحيحة.")
    for row in db.all_("SELECT * FROM media WHERE user_id = ?", (u["id"],)):
        media.media_path(row).unlink(missing_ok=True)
    shutil.rmtree(config.settings.data_dir / "attachments" / u["id"], ignore_errors=True)
    db.run("DELETE FROM users WHERE id = ?", (u["id"],))  # cascades to all user data
    resp = ok()
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---- profile, dashboard, notifications -----------------------------------------------

PROFILE_FIELDS = ("business_name", "industry", "description", "audience", "products", "tone", "city", "contact", "brand_color", "faq")


@api()
async def profile(request: Request):
    u = U(request)
    if request.method == "PUT":
        b = B(request)
        vals = {k: str(b.get(k, ""))[:4000] for k in PROFILE_FIELDS}
        if vals["brand_color"] and not re.fullmatch(r"#[0-9a-fA-F]{6}", vals["brand_color"]):
            vals["brand_color"] = "#6d3df5"
        if db.one("SELECT user_id FROM business_profiles WHERE user_id = ?", (u["id"],)):
            db.update("business_profiles", {"user_id": u["id"]}, {**vals, "updated_at": db.now()})
        else:
            db.insert("business_profiles", {"user_id": u["id"], **vals, "updated_at": db.now()})
    p = db.one("SELECT * FROM business_profiles WHERE user_id = ?", (u["id"],)) or {k: "" for k in PROFILE_FIELDS}
    return ok({k: p.get(k, "") for k in PROFILE_FIELDS})


@api()
async def dashboard(request: Request):
    uid = U(request)["id"]
    count = lambda sql: db.one(sql, (uid,))["n"]  # noqa: E731
    return ok({
        "user": public_user(U(request)),
        "counts": {
            "projects": count("SELECT COUNT(*) n FROM projects WHERE user_id = ? AND status = 'active'"),
            "open_tasks": count("SELECT COUNT(*) n FROM tasks WHERE user_id = ? AND status != 'done'"),
            "done_tasks": count("SELECT COUNT(*) n FROM tasks WHERE user_id = ? AND status = 'done'"),
            "approvals": count("SELECT COUNT(*) n FROM approvals WHERE user_id = ? AND status = 'pending'"),
            "scheduled_posts": count("SELECT COUNT(*) n FROM posts WHERE user_id = ? AND status = 'scheduled'"),
            "published_posts": count("SELECT COUNT(*) n FROM posts WHERE user_id = ? AND status IN ('published','partial')"),
            "sites": count("SELECT COUNT(*) n FROM sites WHERE user_id = ?"),
            "automations": count("SELECT COUNT(*) n FROM automations WHERE user_id = ? AND status = 'active'"),
            "channels": count("SELECT COUNT(*) n FROM channels WHERE user_id = ?"),
            "contacts": count("SELECT COUNT(*) n FROM contacts WHERE user_id = ?"),
        },
        "profile_complete": bool(db.one("SELECT business_name FROM business_profiles WHERE user_id = ? AND business_name != ''", (uid,))),
        "activity": db.all_("SELECT kind, summary, created_at FROM activity WHERE user_id = ? ORDER BY created_at DESC LIMIT 15", (uid,)),
        "conversations": db.all_("SELECT id, persona, title, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT 6", (uid,)),
        "usage": billing.usage_summary(uid),
    })


@api()
async def notifications(request: Request):
    uid = U(request)["id"]
    if request.method == "POST":
        db.run("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (db.now(), uid))
    rows = db.all_("SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 30", (uid,))
    return ok({"items": rows, "unread": sum(1 for r in rows if not r["read_at"])})


async def plans_list(request: Request):
    return ok([{"id": k, "name": v["name_ar"], "price_sar": v["price_sar"], "limits": v["limits"]} for k, v in billing.PLANS.items()])


async def personas_list(request: Request):
    return ok([{"id": k, **{f: PERSONAS[k][f] for f in ("name", "name_en", "role", "role_en", "color", "tagline", "welcome", "services")}} for k in ORDER])


# ---- conversations ----------------------------------------------------------------

@api()
async def conversations(request: Request):
    u = U(request)
    if request.method == "POST":
        persona = B(request).get("persona")
        if persona not in PERSONAS:
            raise ApiError("invalid_persona")
        conv = {"id": db.new_id(), "user_id": u["id"], "persona": persona, "title": "", "created_at": db.now(), "updated_at": db.now()}
        db.insert("conversations", conv)
        return ok(conv, 201)
    persona = request.query_params.get("persona")
    if persona:
        rows = db.all_("SELECT id, persona, title, updated_at FROM conversations WHERE user_id = ? AND persona = ? ORDER BY updated_at DESC LIMIT 50", (u["id"], persona))
    else:
        rows = db.all_("SELECT id, persona, title, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT 50", (u["id"],))
    return ok(rows)


def _conv(request: Request) -> dict:
    c = db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ?", (request.path_params["cid"], U(request)["id"]))
    if not c:
        raise ApiError("not_found", 404)
    return c


@api()
async def conversation(request: Request):
    c = _conv(request)
    if request.method == "DELETE":
        old = db.all_("SELECT user_id, content FROM messages WHERE conversation_id = ?", (c["id"],))
        for row in old:
            data = db.jload(row["content"], {})
            for attachment in data.get("attachments", []) if isinstance(data, dict) else []:
                if isinstance(attachment, dict):
                    attachment_service.cleanup(row["user_id"], attachment)
        db.run("DELETE FROM conversations WHERE id = ?", (c["id"],))
        return ok()
    msgs = db.all_("SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at", (c["id"],))
    for m in msgs:
        data = db.jload(m.pop("content"), {})
        m["text"] = data.get("text", "")
        m["attachments"] = [attachment_service.public_metadata(a, c["id"]) for a in data.get("attachments", []) if isinstance(a, dict)]
        m["actions"] = [{"tool": a["tool"], "label": agent.TOOL_LABELS.get(a["tool"], a["tool"]), "ok": a["ok"],
                         "approval": isinstance(a.get("result"), dict) and a["result"].get("status") == "pending_client_approval"} for a in data.get("actions", [])]
    return ok({"conversation": c, "messages": msgs})


@api()
async def conversation_attachment(request: Request):
    c = _conv(request)
    aid = request.path_params["aid"]
    target = None
    rows = db.all_("SELECT user_id, content FROM messages WHERE conversation_id = ? AND user_id = ? AND role = 'user'", (c["id"], U(request)["id"]))
    for row in rows:
        data = db.jload(row["content"], {})
        for attachment in data.get("attachments", []) if isinstance(data, dict) else []:
            if isinstance(attachment, dict) and attachment.get("id") == aid:
                target = attachment
                break
        if target:
            break
    if not target:
        raise ApiError("not_found", 404)
    path = attachment_service.storage_path(U(request)["id"], target)
    if not path.is_file():
        raise ApiError("not_found", 404)
    filename = str(target.get("filename", "attachment")).replace("\r", "").replace("\n", "").replace('"', "")
    from urllib.parse import quote
    disposition = "inline" if target.get("kind") == "image" or target.get("content_type") == "application/pdf" else "attachment"
    return Response(path.read_bytes(), media_type=target.get("content_type", "application/octet-stream"), headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}", "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
    })


@api(max_body=20_000_000)
async def send_message(request: Request):
    u, c = U(request), _conv(request)
    saved: list[dict] = []
    text = ""
    if "multipart/form-data" in request.headers.get("content-type", "").lower():
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > attachment_service.MAX_TOTAL_SIZE + 1_000_000:
            raise ApiError("payload_too_large", 413, "إجمالي المرفقات يجب ألا يتجاوز 16 ميغابايت.")
        try:
            form = await request.form(max_files=10, max_fields=10, max_part_size=100_000)
        except Exception as exc:  # noqa: BLE001
            raise ApiError("invalid_upload", message="تعذّر قراءة المرفقات. جرّبي ملفات أصغر.") from exc
        text = str(form.get("text", "")).strip()
        uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile) and item.filename]
        if len(uploads) > attachment_service.MAX_FILES:
            raise ApiError("too_many_files", message="يمكن إرفاق 5 ملفات كحد أقصى في الرسالة الواحدة.")
        total = 0
        try:
            for upload in uploads:
                raw = await upload.read(attachment_service.MAX_FILE_SIZE + 1)
                total += len(raw)
                if len(raw) > attachment_service.MAX_FILE_SIZE:
                    raise attachment_service.AttachmentError("حجم كل ملف يجب ألا يتجاوز 8 ميغابايت.")
                if total > attachment_service.MAX_TOTAL_SIZE:
                    raise attachment_service.AttachmentError("إجمالي المرفقات في الرسالة يجب ألا يتجاوز 16 ميغابايت.")
                saved.append(attachment_service.prepare_upload(u["id"], upload.filename or "file", upload.content_type or "", raw))
        except attachment_service.AttachmentError as exc:
            for item in saved:
                attachment_service.cleanup(u["id"], item)
            raise ApiError("invalid_attachment", message=str(exc)) from exc
        except Exception:
            for item in saved:
                attachment_service.cleanup(u["id"], item)
            raise
    else:
        text = str(B(request).get("text", "")).strip()
    if not text and not saved:
        raise ApiError("empty", message="اكتبي رسالة أو أرفقي ملفًا.")
    if not text:
        text = "يرجى فحص المرفقات ومساعدتي بناءً عليها."
    if len(text) > 20_000:
        for item in saved:
            attachment_service.cleanup(u["id"], item)
        raise ApiError("too_long")
    if not limiter.allow(f"chat:{u['id']}", 20, 60):
        for item in saved:
            attachment_service.cleanup(u["id"], item)
        raise ApiError("rate_limited", 429, "رسائل كثيرة بسرعة، تمهّل دقيقة.")

    def gen():
        for ev in agent.run_turn(u, c, text, attachments=saved):
            yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(iterate_in_threadpool(gen()), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---- core / team ---------------------------------------------------------------------

@api()
async def core_route(request: Request):
    return ok(await run_in_threadpool(core.route, str(B(request).get("text", ""))[:3000]))


@api()
async def core_team(request: Request):
    goal = str(B(request).get("goal", "")).strip()
    if len(goal) < 5:
        raise ApiError("invalid_input", message="اكتب هدفك بوضوح.")
    billing.check(U(request)["id"], "messages")
    billing.consume(U(request)["id"], "messages")
    try:
        return ok(await run_in_threadpool(core.team_plan, U(request), goal), 201)
    except Exception as exc:  # noqa: BLE001
        from app import llm
        if isinstance(exc, llm.ProviderNotConfigured):
            raise ApiError("not_configured", 503, "خدمة الفريق لم تُفعَّل بعد على الخادم.") from exc
        if isinstance(exc, (llm.ProviderError, json.JSONDecodeError)):
            raise ApiError("provider_error", 502, "تعذّر تجهيز الخطة الآن، جرّب مرة ثانية.") from exc
        raise


@api()
async def task_run(request: Request):
    return ok(core.run_task(U(request), request.path_params["tid"]), 202)


# ---- projects & tasks -----------------------------------------------------------------

@api()
async def projects_ep(request: Request):
    u = U(request)
    if request.method == "POST":
        b = B(request)
        return ok(projects.create_project(u["id"], str(b.get("name", "")), str(b.get("description", "")), str(b.get("goal", ""))), 201)
    return ok(projects.list_projects(u["id"]))


@api()
async def project_ep(request: Request):
    u = U(request)
    p = projects.get(u["id"], request.path_params["pid"])
    if not p:
        raise ApiError("not_found", 404)
    if request.method == "DELETE":
        db.update("projects", {"id": p["id"]}, {"status": "archived", "updated_at": db.now()})
        return ok()
    if request.method == "PATCH":
        b = B(request)
        vals = {k: str(b[k])[:4000] for k in ("name", "description", "goal") if k in b}
        if vals:
            db.update("projects", {"id": p["id"]}, {**vals, "updated_at": db.now()})
        p = projects.get(u["id"], p["id"])
    return ok({"project": p, "tasks": projects.tasks_for(u["id"], p["id"]), "calendar_url": calendar_url(u, p["id"])})


@api()
async def tasks_ep(request: Request):
    u, b = U(request), B(request)
    t = projects.add_task(u["id"], request.path_params["pid"], str(b.get("title", "")), str(b.get("details", "")), b.get("assignee", "me"), b.get("due_date"), b.get("priority", "medium"))
    return ok(t, 201)


@api()
async def task_ep(request: Request):
    u = U(request)
    tid = request.path_params["tid"]
    if request.method == "DELETE":
        if not db.run("DELETE FROM tasks WHERE id = ? AND user_id = ?", (tid, u["id"])):
            raise ApiError("not_found", 404)
        return ok()
    return ok(projects.update_task(u["id"], tid, **B(request)))


async def calendar_feed(request: Request):
    uid, token = request.path_params["uid"], request.path_params["token"]
    if not hmac.compare_digest(token, calendar_token(uid)):
        return PlainTextResponse("not found", 404)
    body = await run_in_threadpool(projects.ics, uid, request.query_params.get("project"))
    return Response(body, media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="sahm.ics"'})


# ---- approvals ----------------------------------------------------------------------------

@api()
async def approvals_ep(request: Request):
    uid = U(request)["id"]
    status = request.query_params.get("status", "pending")
    rows = db.all_("SELECT * FROM approvals WHERE user_id = ? AND (? = 'all' OR status = ?) ORDER BY created_at DESC LIMIT 100", (uid, status, status))
    out = []
    for r in rows:
        payload = db.jload(r["payload"], {})
        item = {k: r[k] for k in ("id", "persona", "action", "title", "status", "created_at", "decided_at")}
        item["result"] = db.jload(r["result"], None)
        if r["action"] == "publish_post":
            p = posts.get(uid, payload.get("post_id", ""))
            item["post"] = posts.to_public(p) if p else None
        elif r["action"] == "publish_site":
            s = sites.get(uid, payload.get("site_id", ""))
            item["site"] = sites.to_public(s) if s else None
        elif r["action"] in ("activate_automation", "push_n8n"):
            a = automations.get(uid, payload.get("automation_id", ""))
            item["automation"] = automations.to_public(a) if a else None
        elif r["action"] == "reply_comment":
            item["message"] = payload.get("message")
        elif r["action"] == "whatsapp_campaign":
            item["campaign"] = {k: payload.get(k) for k in ("template", "lang", "tag")}
        out.append(item)
    return ok(out)


@api()
async def approval_decide(request: Request):
    uid, aid, decision = U(request)["id"], request.path_params["aid"], request.path_params["decision"]
    if decision == "approve":
        return ok(await run_in_threadpool(tools.execute_approval, uid, aid))
    if decision == "reject":
        tools.reject_approval(uid, aid)
        return ok({"status": "rejected"})
    raise ApiError("not_found", 404)


# ---- channels -----------------------------------------------------------------------------

@api()
async def channels_ep(request: Request):
    uid = U(request)["id"]
    return ok({
        "channels": [ch.public(c) for c in ch.list_for(uid)],
        "available": {
            "meta": meta_api.configured(), "tiktok": tiktok.configured(), "telegram": True, "whatsapp": True, "n8n": True,
            "whatsapp_webhook": f"{config.settings.base_url}/hooks/whatsapp" if config.settings.whatsapp_verify_token else None,
        },
    })


@api()
async def channel_delete(request: Request):
    uid = U(request)["id"]
    c = ch.get(uid, request.path_params["chid"])
    if not c:
        raise ApiError("not_found", 404)
    if c["platform"] == "telegram":
        try:
            telegram.delete_webhook(ch.secret(c))
        except ChannelError:
            pass
    db.run("DELETE FROM channels WHERE id = ?", (c["id"],))
    return ok()


def _new_state(uid: str, provider: str) -> str:
    state = random_token(24)
    db.insert("oauth_states", {"state": state, "user_id": uid, "provider": provider, "extra": "{}", "created_at": db.now()})
    return state


def _take_state(state: str, provider: str) -> str | None:
    row = db.one("SELECT * FROM oauth_states WHERE state = ? AND provider = ?", (state, provider))
    if not row or row["created_at"] < db.now() - 3600:
        return None
    db.run("DELETE FROM oauth_states WHERE state = ?", (state,))
    return row["user_id"]


@api()
async def meta_start(request: Request):
    if not meta_api.configured():
        raise ApiError("not_configured", 503, "ربط Meta لم يُفعَّل على الخادم بعد.")
    return ok({"url": meta_api.authorize_url(_new_state(U(request)["id"], "meta"))})


async def meta_callback(request: Request):
    q = request.query_params
    uid = _take_state(q.get("state", ""), "meta")
    if not uid or not q.get("code"):
        return RedirectResponse("/app#/channels?error=meta")
    try:
        user_token = await run_in_threadpool(meta_api.exchange_code, q["code"])
        pages = await run_in_threadpool(meta_api.list_pages, user_token)
        n = 0
        for p in pages:
            ch.upsert(uid, "facebook", p["id"], p["name"], p["access_token"], {"page_name": p["name"]})
            n += 1
            ig = p.get("instagram_business_account")
            if ig:
                ch.upsert(uid, "instagram", ig["id"], "@" + ig.get("username", ig["id"]), p["access_token"], {"username": ig.get("username"), "page_id": p["id"]})
                n += 1
        return RedirectResponse(f"/app#/channels?connected={n}")
    except (ChannelError, billing.LimitReached) as exc:
        log.warning("meta connect failed: %s", exc)
        return RedirectResponse("/app#/channels?error=meta_failed")


@api()
async def tiktok_start(request: Request):
    if not tiktok.configured():
        raise ApiError("not_configured", 503, "ربط تيك توك لم يُفعَّل على الخادم بعد.")
    return ok({"url": tiktok.authorize_url(_new_state(U(request)["id"], "tiktok"))})


async def tiktok_callback(request: Request):
    q = request.query_params
    uid = _take_state(q.get("state", ""), "tiktok")
    if not uid or not q.get("code"):
        return RedirectResponse("/app#/channels?error=tiktok")
    try:
        tok = await run_in_threadpool(tiktok.exchange_code, q["code"])
        info = await run_in_threadpool(tiktok.user_info, tok["access_token"])
        ch.upsert(uid, "tiktok", tok.get("open_id") or info.get("open_id", "me"), info.get("display_name", "TikTok"), db.jdump(tok), {"username": info.get("display_name")})
        return RedirectResponse("/app#/channels?connected=1")
    except (ChannelError, billing.LimitReached) as exc:
        log.warning("tiktok connect failed: %s", exc)
        return RedirectResponse("/app#/channels?error=tiktok_failed")


@api()
async def connect_telegram(request: Request):
    uid, b = U(request)["id"], B(request)
    token = str(b.get("bot_token", "")).strip()
    chat = str(b.get("chat_id", "")).strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise ApiError("invalid_input", message="توكن البوت غير صحيح (من BotFather).")
    bot = await run_in_threadpool(telegram.get_me, token)
    meta = {"bot_username": bot.get("username"), "webhook_secret": random_token(24), "kind": "channel" if chat else "bot"}
    name = "@" + bot.get("username", "bot")
    if chat:
        info = await run_in_threadpool(telegram.get_chat, token, chat)
        meta.update({"chat_id": str(info["id"]), "chat_title": info.get("title") or info.get("username")})
        name = info.get("title") or chat
    row = ch.upsert(uid, "telegram", f"{bot['id']}:{meta.get('chat_id', 'bot')}", name, token, meta)
    return ok(ch.public(row), 201)


@api()
async def connect_whatsapp(request: Request):
    uid, b = U(request)["id"], B(request)
    pnid, token, waba = str(b.get("phone_number_id", "")).strip(), str(b.get("access_token", "")).strip(), str(b.get("waba_id", "")).strip()
    if not pnid.isdigit() or len(token) < 20:
        raise ApiError("invalid_input", message="أدخل Phone Number ID والتوكن الدائم من Meta.")
    info = await run_in_threadpool(whatsapp.verify_number, pnid, token)
    row = ch.upsert(uid, "whatsapp", pnid, info.get("verified_name") or info.get("display_phone_number", "WhatsApp"), token,
                    {"display_phone": info.get("display_phone_number"), "waba_id": waba})
    return ok(ch.public(row), 201)


@api()
async def connect_n8n(request: Request):
    uid, b = U(request)["id"], B(request)
    base, key = str(b.get("base_url", "")).strip().rstrip("/"), str(b.get("api_key", "")).strip()
    if not base or not key:
        raise ApiError("invalid_input", message="أدخل رابط n8n ومفتاح API.")
    await run_in_threadpool(n8n_api.verify, base, key)
    row = ch.upsert(uid, "n8n", base, base.replace("https://", ""), key, {"base_url": base})
    return ok(ch.public(row), 201)


# ---- media & posts -------------------------------------------------------------------

@api(max_body=0)
async def media_ep(request: Request):
    uid = U(request)["id"]
    if request.method == "POST":
        form = await request.form(max_part_size=media.MAX_VIDEO)
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise ApiError("no_file")
        data = await f.read()
        row = await run_in_threadpool(media.save, uid, data, f.content_type or "")
        return ok({"id": row["id"], "kind": row["kind"], "url": media.public_url(row)}, 201)
    rows = db.all_("SELECT * FROM media WHERE user_id = ? ORDER BY created_at DESC LIMIT 60", (uid,))
    return ok([{"id": r["id"], "kind": r["kind"], "url": media.public_url(r), "created_at": r["created_at"]} for r in rows])


@api()
async def design_ep(request: Request):
    uid, b = U(request)["id"], B(request)
    prof = db.one("SELECT business_name, brand_color FROM business_profiles WHERE user_id = ?", (uid,)) or {}
    row = await run_in_threadpool(media.design_post, uid, str(b.get("headline", ""))[:200] or "شهم", str(b.get("subline", ""))[:300],
                                  str(b.get("footer") or prof.get("business_name", ""))[:60], b.get("style", "bold"), prof.get("brand_color") or "#6d3df5")
    return ok({"id": row["id"], "kind": "image", "url": media.public_url(row)}, 201)


async def media_public(request: Request):
    token = request.path_params["token"].split(".")[0]
    row = db.one("SELECT * FROM media WHERE public_token = ?", (token,))
    if not row:
        return PlainTextResponse("not found", 404)
    return FileResponse(media.media_path(row), media_type=row["content_type"], headers={"Cache-Control": "public, max-age=86400"})


@api()
async def posts_ep(request: Request):
    uid = U(request)["id"]
    if request.method == "POST":
        b = B(request)
        when = tools.parse_local(b.get("scheduled_at")) if b.get("scheduled_at") else None
        p = posts.create(uid, str(b.get("caption", "")), list(b.get("media_ids") or []), list(b.get("channel_ids") or []), when)
        if b.get("submit"):
            p = posts.approve(uid, p["id"])  # the client is creating it themselves = approved
        return ok(posts.to_public(p), 201)
    rows = db.all_("SELECT * FROM posts WHERE user_id = ? ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT 100", (uid,))
    return ok([posts.to_public(r) for r in rows])


@api()
async def post_ep(request: Request):
    uid = U(request)["id"]
    p = posts.get(uid, request.path_params["post_id"])
    if not p:
        raise ApiError("not_found", 404)
    action = request.path_params.get("action")
    if request.method == "DELETE":
        if p["status"] == "publishing":
            raise ApiError("busy")
        db.run("DELETE FROM posts WHERE id = ?", (p["id"],))
        return ok()
    if action == "publish":
        if p["status"] in ("published", "publishing"):
            raise ApiError("already_published")
        db.update("posts", {"id": p["id"]}, {"scheduled_at": db.now()})
        p = posts.approve(uid, p["id"])
        await run_in_threadpool(posts.publish, p)
        return ok(posts.to_public(posts.get(uid, p["id"])))
    if action == "insights":
        await run_in_threadpool(posts.refresh_insights, p)
        return ok(posts.to_public(posts.get(uid, p["id"])))
    if request.method == "PATCH":
        if p["status"] not in ("draft", "pending_approval", "scheduled", "failed"):
            raise ApiError("not_editable")
        b = B(request)
        vals: dict = {"updated_at": db.now()}
        if "caption" in b:
            vals["caption"] = str(b["caption"])[:5000]
        if "scheduled_at" in b:
            vals["scheduled_at"] = tools.parse_local(b["scheduled_at"]) if b["scheduled_at"] else None
        if "channel_ids" in b:
            vals["channel_ids"] = db.jdump([c for c in b["channel_ids"] if ch.get(uid, c)])
        if "media_ids" in b:
            vals["media_ids"] = db.jdump([m["id"] for m in media.get_owned(uid, b["media_ids"])])
        db.update("posts", {"id": p["id"]}, vals)
    return ok(posts.to_public(posts.get(uid, p["id"])))


# ---- contacts -----------------------------------------------------------------------------

@api(max_body=5_000_000)
async def contacts_ep(request: Request):
    uid = U(request)["id"]
    if request.method == "POST":
        b = B(request)
        rows = []
        if b.get("csv"):
            reader = csv.DictReader(io.StringIO(str(b["csv"])))
            for r in reader:
                rows.append({k.strip().lower(): (v or "").strip() for k, v in r.items() if k})
        else:
            rows = [b]
        added = 0
        for r in rows[:5000]:
            phone = re.sub(r"\D", "", str(r.get("phone", "")))
            email = str(r.get("email", "")).strip().lower()
            if not phone and not email:
                continue
            db.insert("contacts", {"id": db.new_id(), "user_id": uid, "name": str(r.get("name", ""))[:120], "phone": phone or None, "email": email or None,
                                   "tags": str(r.get("tags", ""))[:200], "opted_in": 1 if str(r.get("opted_in", "")).lower() in ("1", "true", "yes", "نعم") else 0,
                                   "created_at": db.now(), "source": "import" if b.get("csv") else "manual", "note": str(r.get("note", ""))[:500]})
            added += 1
        return ok({"added": added}, 201)
    rows = db.all_("SELECT * FROM contacts WHERE user_id = ? ORDER BY created_at DESC LIMIT 500", (uid,))
    return ok(rows)


@api()
async def contact_ep(request: Request):
    uid = U(request)["id"]
    cid = request.path_params["cid"]
    if request.method == "DELETE":
        db.run("DELETE FROM contacts WHERE id = ? AND user_id = ?", (cid, uid))
        return ok()
    b = B(request)
    vals = {}
    if "opted_in" in b:
        vals["opted_in"] = 1 if b["opted_in"] else 0
    if "tags" in b:
        vals["tags"] = str(b["tags"])[:200]
    if vals:
        db.run(f"UPDATE contacts SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ? AND user_id = ?", (*vals.values(), cid, uid))
    return ok()


# ---- sites ---------------------------------------------------------------------------------

@api()
async def sites_ep(request: Request):
    uid = U(request)["id"]
    rows = db.all_("SELECT * FROM sites WHERE user_id = ? ORDER BY updated_at DESC", (uid,))
    return ok([sites.to_public(r) for r in rows])


@api(max_body=600_000)
async def site_ep(request: Request):
    uid = U(request)["id"]
    s = sites.get(uid, request.path_params["sid"])
    if not s:
        raise ApiError("not_found", 404)
    action = request.path_params.get("action")
    if request.method == "DELETE":
        db.run("DELETE FROM sites WHERE id = ?", (s["id"],))
        return ok()
    if action == "publish":
        return ok(sites.to_public(sites.publish(uid, s["id"])))
    if action == "unpublish":
        sites.unpublish(uid, s["id"])
        return ok(sites.to_public(sites.get(uid, s["id"])))
    if request.method == "PUT":
        b = B(request)
        s = sites.update(uid, s["id"], str(b.get("html", "")), b.get("title"))
    return ok(sites.to_public(s, with_html=True))


async def site_zip(request: Request):
    user = read_session(request.cookies.get(SESSION_COOKIE))
    s = sites.get(user["id"], request.path_params["sid"]) if user else None
    if not s:
        return PlainTextResponse("not found", 404)
    return Response(sites.zip_bytes(s), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{s["slug"]}.zip"'})


async def site_preview(request: Request):
    user = read_session(request.cookies.get(SESSION_COOKIE))
    s = sites.get(user["id"], request.path_params["sid"]) if user else None
    if not s:
        return PlainTextResponse("not found", 404)
    return HTMLResponse(s["html"], headers={"Content-Security-Policy": sites.SANDBOX_CSP, "X-Robots-Tag": "noindex"})


async def site_public(request: Request):
    s = db.one("SELECT * FROM sites WHERE slug = ? AND status = 'published'", (request.path_params["slug"],))
    if not s:
        return HTMLResponse("<h1 style='font-family:sans-serif;text-align:center;margin-top:20vh'>404</h1>", 404)
    return HTMLResponse(s["published_html"], headers={"Content-Security-Policy": sites.SANDBOX_CSP, "Cache-Control": "public, max-age=60"})


async def site_form(request: Request):
    slug = request.path_params["slug"]
    s = db.one("SELECT * FROM sites WHERE slug = ? AND status = 'published'", (slug,))
    if not s:
        return PlainTextResponse("not found", 404)
    ip = request.client.host if request.client else "?"
    if not limiter.allow(f"form:{slug}:{ip}", 5, 600):
        return PlainTextResponse("too many requests", 429)
    if "json" in request.headers.get("content-type", ""):
        data = await request.json()
    else:
        data = dict(await request.form())
    data = {str(k)[:40]: str(v)[:2000] for k, v in list(data.items())[:20] if not hasattr(v, "read")}
    if data.get("website") or data.get("_gotcha"):  # honeypot
        return HTMLResponse(_thanks())
    phone = re.sub(r"\D", "", data.get("phone", ""))
    email = data.get("email", "").strip().lower()
    db.insert("contacts", {"id": db.new_id(), "user_id": s["user_id"], "name": data.get("name", "")[:120], "phone": phone or None, "email": email or None,
                           "tags": "موقع", "opted_in": 0, "created_at": db.now(), "source": f"site:{s['slug']}", "note": data.get("message", "")[:1000]})
    db.notify(s["user_id"], f"طلب جديد من موقع «{s['title']}»", f"{data.get('name', '')} — {data.get('message', '')[:200]}", "#/contacts")
    await run_in_threadpool(automations.fire_event, s["user_id"], "site_form", {"form": data, "site": s["title"], "text": data.get("message", ""), "name": data.get("name", "")})
    if "json" in request.headers.get("accept", ""):
        return ok({"ok": True})
    return HTMLResponse(_thanks())


def _thanks() -> str:
    return ('<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font-family:Tahoma,sans-serif;display:grid;place-items:center;min-height:90vh;text-align:center">'
            '<div><h2>تم استلام طلبك ✓</h2><p>سيتواصل معك الفريق قريبًا.</p><a href="javascript:history.back()">رجوع</a></div></body></html>')


# ---- automations ----------------------------------------------------------------------------

@api()
async def automations_ep(request: Request):
    uid = U(request)["id"]
    rows = db.all_("SELECT * FROM automations WHERE user_id = ? ORDER BY updated_at DESC", (uid,))
    return ok([automations.to_public(r) for r in rows])


@api()
async def automation_ep(request: Request):
    uid = U(request)["id"]
    a = automations.get(uid, request.path_params["aid"])
    if not a:
        raise ApiError("not_found", 404)
    action = request.path_params.get("action")
    if request.method == "DELETE":
        automations.pause(uid, a["id"])
        db.run("DELETE FROM automations WHERE id = ?", (a["id"],))
        return ok()
    if action == "activate":
        automations.activate(uid, a["id"])
    elif action == "pause":
        automations.pause(uid, a["id"])
    elif action == "run":
        if a["status"] != "active":
            raise ApiError("not_active", message="فعّل الأتمتة أولًا.")
        res = await run_in_threadpool(automations.run, a, "manual", {"text": "تجربة يدوية", "name": U(request)["name"]})
        return ok(res)
    a = automations.get(uid, a["id"])
    runs = db.all_("SELECT id, trigger_kind, status, log, started_at, finished_at FROM automation_runs WHERE automation_id = ? ORDER BY started_at DESC LIMIT 20", (a["id"],))
    for r in runs:
        r["log"] = db.jload(r["log"], [])
    return ok({**automations.to_public(a), "runs": runs})


async def webhook_trigger(request: Request):
    a = db.one("SELECT * FROM automations WHERE webhook_token = ?", (request.path_params["token"],))
    if not a or a["status"] != "active" or db.jload(a["trigger"], {}).get("type") != "webhook":
        return JSONResponse({"error": "not_found"}, 404)
    if not limiter.allow(f"hook:{a['id']}", 120, 60):
        return JSONResponse({"error": "rate_limited"}, 429)
    raw = await request.body()
    if len(raw) > 1_000_000:
        return JSONResponse({"error": "too_large"}, 413)
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"raw": raw.decode(errors="replace")[:5000]}
    payload = {"body": body, "query": dict(request.query_params), "text": body.get("text", "") if isinstance(body, dict) else ""}
    result = await run_in_threadpool(automations.run, a, "webhook", payload)
    return JSONResponse({"run": result["id"], "status": result["status"]})


async def whatsapp_hook(request: Request):
    s = config.settings
    if request.method == "GET":
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and s.whatsapp_verify_token and hmac.compare_digest(q.get("hub.verify_token", ""), s.whatsapp_verify_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        return PlainTextResponse("forbidden", 403)
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(s.meta_app_secret.encode(), raw, hashlib.sha256).hexdigest() if s.meta_app_secret else ""
    if not expected or not hmac.compare_digest(sig, expected):
        return PlainTextResponse("invalid signature", 401)
    for m in whatsapp.parse_inbound(json.loads(raw or b"{}")):
        if db.one("SELECT id FROM webhook_events WHERE id = ?", (f"wa:{m['id']}",)):
            continue
        db.insert("webhook_events", {"id": f"wa:{m['id']}", "source": "whatsapp", "created_at": db.now()})
        for c in db.all_("SELECT * FROM channels WHERE platform = 'whatsapp' AND external_id = ?", (m["phone_number_id"],)):
            _upsert_contact(c["user_id"], m["name"], m["from"], "واتساب")
            await run_in_threadpool(automations.handle_inbound, c, "whatsapp_inbound", {"text": m["text"], "from": m["from"], "name": m["name"]})
    return PlainTextResponse("ok")


def _upsert_contact(user_id: str, name: str, phone: str, tag: str) -> None:
    if not phone or db.one("SELECT id FROM contacts WHERE user_id = ? AND phone = ?", (user_id, phone)):
        return
    # A customer who messages the business has opted in to conversation replies (not marketing).
    db.insert("contacts", {"id": db.new_id(), "user_id": user_id, "name": name[:120], "phone": phone, "email": None, "tags": tag,
                           "opted_in": 0, "created_at": db.now(), "source": tag, "note": ""})


async def telegram_hook(request: Request):
    c = db.one("SELECT * FROM channels WHERE id = ? AND platform = 'telegram'", (request.path_params["chid"],))
    if not c:
        return PlainTextResponse("not found", 404)
    secret = ch.meta(c).get("webhook_secret", "")
    if not secret or not hmac.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), secret):
        return PlainTextResponse("forbidden", 403)
    upd = await request.json()
    uid_key = f"tg:{c['id']}:{upd.get('update_id')}"
    if db.one("SELECT id FROM webhook_events WHERE id = ?", (uid_key,)):
        return PlainTextResponse("ok")
    db.insert("webhook_events", {"id": uid_key, "source": "telegram", "created_at": db.now()})
    msg = upd.get("message") or {}
    if msg.get("text") and msg.get("chat", {}).get("type") == "private":
        name = " ".join(x for x in (msg.get("from", {}).get("first_name"), msg.get("from", {}).get("last_name")) if x)
        await run_in_threadpool(automations.handle_inbound, c, "telegram_inbound", {"text": msg["text"], "from": str(msg["chat"]["id"]), "chat_id": str(msg["chat"]["id"]), "name": name})
    return PlainTextResponse("ok")


# ---- billing --------------------------------------------------------------------------------

@api()
async def billing_ep(request: Request):
    uid = U(request)["id"]
    sub = billing.subscription(uid)
    return ok({
        "subscription": sub, "usage": billing.usage_summary(uid), "payments_enabled": bool(config.settings.moyasar_secret_key),
        "plans": [{"id": k, "name": v["name_ar"], "price_sar": v["price_sar"], "limits": v["limits"]} for k, v in billing.PLANS.items()],
        "invoices": db.all_("SELECT id, plan, amount_halalas, status, url, created_at, paid_at FROM invoices WHERE user_id = ? ORDER BY created_at DESC LIMIT 20", (uid,)),
    })


@api()
async def checkout(request: Request):
    plan = B(request).get("plan")
    try:
        return ok(await run_in_threadpool(billing.create_invoice, U(request), plan), 201)
    except billing.PaymentsNotConfigured as exc:
        raise ApiError("payments_not_configured", 503, "الدفع الإلكتروني لم يُفعَّل بعد على الخادم.") from exc
    except RuntimeError as exc:
        raise ApiError("payment_provider_error", 502, "تعذّر إنشاء الفاتورة، جرّب بعد قليل.") from exc


async def billing_webhook(request: Request):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return PlainTextResponse("bad request", 400)
    if not billing.verify_webhook_secret(event.get("secret_token")):
        return PlainTextResponse("forbidden", 403)
    if event.get("type") == "payment_paid":
        invoice_id = (event.get("data") or {}).get("invoice_id")
        if invoice_id:
            try:
                await run_in_threadpool(billing.settle_invoice, invoice_id)
            except Exception as exc:  # noqa: BLE001 — Moyasar retries on non-2xx
                log.exception("settle failed: %s", exc)
                return PlainTextResponse("retry", 500)
    return PlainTextResponse("ok")


async def billing_callback(request: Request):
    """Invoice callback_url (Moyasar notifies here when an invoice is paid)."""
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = dict(await request.form())
        inv = data.get("id")
        if inv and data.get("status") == "paid":
            try:
                await run_in_threadpool(billing.settle_invoice, str(inv))
            except Exception as exc:  # noqa: BLE001
                log.warning("callback settle failed: %s", exc)
        return PlainTextResponse("ok")
    return RedirectResponse("/app#/billing")


# ---- pages -----------------------------------------------------------------------------------

async def landing(request: Request):
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


async def app_shell(request: Request):
    return FileResponse(WEB / "app.html", headers={"Cache-Control": "no-cache"})


def _legal(name: str):
    async def page(request: Request):
        import datetime as _dt
        html = (WEB / f"{name}.html").read_text(encoding="utf-8")
        contact = config.settings.admin_emails[0] if config.settings.admin_emails else "support@example.com"
        html = html.replace("{DATE}", "2026-10-26").replace("{CONTACT}", contact)
        return HTMLResponse(html)
    return page


async def healthz(request: Request):
    db.one("SELECT 1 AS ok")
    from app import llm
    return ok({"status": "ok", "model_configured": bool(config.settings.anthropic_api_key), "email_configured": bool(config.settings.smtp_host),
               "payments_configured": bool(config.settings.moyasar_secret_key), "meta_configured": meta_api.configured(), "tiktok_configured": tiktok.configured(),
               "provider": type(llm.provider()).__name__})


APP_CSP = ("default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https:")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        path = request.url.path
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if config.settings.is_production:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not (path.startswith("/s/") or path.startswith("/api/sites/") and path.endswith("/preview")):
            resp.headers.setdefault("Content-Security-Policy", APP_CSP)
            resp.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp


class SitesHost:
    """When SAHM_SITES_BASE_URL is a separate host, serve only published sites there."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from urllib.parse import urlparse
        sites_host = urlparse(config.settings.sites_base_url).hostname if config.settings.sites_base_url else None
        if scope["type"] == "http" and sites_host:
            host = dict(scope.get("headers") or []).get(b"host", b"").decode().split(":")[0]
            if host == sites_host:
                path = scope["path"]
                if path.startswith("/f/"):
                    pass
                elif path in ("", "/"):
                    resp = PlainTextResponse("SAHM sites", 200)
                    return await resp(scope, receive, send)
                else:
                    scope = dict(scope, path="/s/" + path.strip("/").split("/")[0], raw_path=None)
        await self.app(scope, receive, send)


def build_app(start_worker: bool | None = None) -> Starlette:
    problems = config.settings.validate()
    if problems:
        raise RuntimeError("Configuration errors:\n- " + "\n- ".join(problems))
    db.init()
    routes = [
        Route("/", landing), Route("/app", app_shell), Route("/healthz", healthz),
        Route("/privacy", _legal("privacy")), Route("/terms", _legal("terms")),
        Route("/api/auth/register", register, methods=["POST"]), Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
        Route("/api/me", me, methods=["GET", "PATCH"]), Route("/api/me/password", change_password, methods=["POST"]),
        Route("/api/me/delete", delete_account, methods=["POST"]),
        Route("/api/profile", profile, methods=["GET", "PUT"]), Route("/api/dashboard", dashboard),
        Route("/api/notifications", notifications, methods=["GET", "POST"]), Route("/api/personas", personas_list), Route("/api/plans", plans_list),
        Route("/api/conversations", conversations, methods=["GET", "POST"]),
        Route("/api/conversations/{cid}/attachments/{aid}", conversation_attachment, methods=["GET"]),
        Route("/api/conversations/{cid}", conversation, methods=["GET", "DELETE"]),
        Route("/api/conversations/{cid}/messages", send_message, methods=["POST"]),
        Route("/api/core/route", core_route, methods=["POST"]), Route("/api/core/team", core_team, methods=["POST"]),
        Route("/api/projects", projects_ep, methods=["GET", "POST"]),
        Route("/api/projects/{pid}", project_ep, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/projects/{pid}/tasks", tasks_ep, methods=["POST"]),
        Route("/api/tasks/{tid}", task_ep, methods=["PATCH", "DELETE"]), Route("/api/tasks/{tid}/run", task_run, methods=["POST"]),
        Route("/cal/{uid}/{token}.ics", calendar_feed),
        Route("/api/approvals", approvals_ep), Route("/api/approvals/{aid}/{decision}", approval_decide, methods=["POST"]),
        Route("/api/channels", channels_ep), Route("/api/channels/{chid}", channel_delete, methods=["DELETE"]),
        Route("/api/channels/meta/start", meta_start, methods=["POST"]), Route("/api/channels/meta/callback", meta_callback),
        Route("/api/channels/tiktok/start", tiktok_start, methods=["POST"]), Route("/api/channels/tiktok/callback", tiktok_callback),
        Route("/api/channels/telegram", connect_telegram, methods=["POST"]), Route("/api/channels/whatsapp", connect_whatsapp, methods=["POST"]),
        Route("/api/channels/n8n", connect_n8n, methods=["POST"]),
        Route("/api/media", media_ep, methods=["GET", "POST"]), Route("/api/media/design", design_ep, methods=["POST"]),
        Route("/m/{token}", media_public),
        Route("/api/posts", posts_ep, methods=["GET", "POST"]),
        Route("/api/posts/{post_id}", post_ep, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/posts/{post_id}/{action}", post_ep, methods=["POST"]),
        Route("/api/contacts", contacts_ep, methods=["GET", "POST"]), Route("/api/contacts/{cid}", contact_ep, methods=["PATCH", "DELETE"]),
        Route("/api/sites", sites_ep), Route("/api/sites/{sid}", site_ep, methods=["GET", "PUT", "DELETE"]),
        Route("/api/sites/{sid}/zip", site_zip), Route("/api/sites/{sid}/preview", site_preview),
        Route("/api/sites/{sid}/{action}", site_ep, methods=["POST"]),
        Route("/s/{slug}", site_public), Route("/f/{slug}", site_form, methods=["POST"]),
        Route("/api/automations", automations_ep), Route("/api/automations/{aid}", automation_ep, methods=["GET", "DELETE"]),
        Route("/api/automations/{aid}/{action}", automation_ep, methods=["POST"]),
        Route("/hooks/whatsapp", whatsapp_hook, methods=["GET", "POST"]), Route("/hooks/telegram/{chid}", telegram_hook, methods=["POST"]),
        Route("/hooks/{token}", webhook_trigger, methods=["GET", "POST"]),
        Route("/api/billing", billing_ep), Route("/api/billing/checkout", checkout, methods=["POST"]),
        Route("/api/billing/webhook", billing_webhook, methods=["POST"]), Route("/api/billing/callback", billing_callback, methods=["GET", "POST"]),
        Mount("/assets", app=StaticFiles(directory=str(WEB / "assets")), name="assets"),
    ]

    async def not_found(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not_found"}, exc.status_code)
        return HTMLResponse("<h1 style='font-family:sans-serif;text-align:center;margin-top:20vh'>404</h1>", exc.status_code)

    run_worker = config.settings.run_worker_in_process if start_worker is None else start_worker

    @contextlib.asynccontextmanager
    async def lifespan(app):
        stop = None
        if run_worker:
            from app import worker
            stop = worker.start_background()
        yield
        if stop:
            stop.set()

    return Starlette(routes=routes, middleware=[Middleware(SitesHost), Middleware(SecurityHeaders)], exception_handlers={404: not_found, 405: not_found}, lifespan=lifespan)
