"""Persona conversation loop: model ↔ tools, streamed to the client as events."""
from __future__ import annotations

import json
import queue
import threading
from typing import Iterator

from app import attachments as attachment_service, billing, db, llm, tools
from app.personas import COMMON_RULES, PERSONAS
from app.services.automations import business_context

MAX_ROUNDS = 8
HISTORY_MESSAGES = 30

TOOL_LABELS = {
    "get_business_profile": "قراءة ملف النشاط", "save_business_profile": "حفظ معلومات النشاط", "handoff_to_teammate": "تحويل مهمة لزميل",
    "list_channels": "مراجعة الحسابات المربوطة", "design_post_image": "تصميم منشور", "create_post": "تجهيز منشور للنشر",
    "list_posts": "مراجعة المنشورات", "get_post_insights": "جلب أرقام الأداء", "list_recent_comments": "قراءة التعليقات",
    "reply_to_comment": "تجهيز رد على تعليق", "contacts_summary": "مراجعة جهات الاتصال", "list_whatsapp_templates": "قراءة قوالب واتساب",
    "whatsapp_campaign": "تجهيز حملة واتساب", "create_project_plan": "إنشاء خطة المشروع", "list_projects": "مراجعة المشاريع",
    "get_project": "قراءة مهام المشروع", "add_tasks": "إضافة مهام", "update_task": "تحديث مهمة", "send_progress_report": "إرسال تقرير التقدم",
    "schedule_weekly_report": "جدولة التقرير الأسبوعي", "calendar_link": "رابط التقويم", "create_site": "بناء الموقع",
    "update_site": "تعديل الموقع", "get_site": "قراءة الموقع", "list_sites": "مراجعة المواقع", "request_publish_site": "طلب نشر الموقع",
    "create_automation": "بناء الأتمتة", "list_automations": "مراجعة الأتمتات", "automation_runs": "قراءة سجل التشغيل",
    "run_automation_now": "تشغيل الأتمتة", "push_to_n8n": "تجهيز الإرسال إلى n8n",
}


def system_prompt(user: dict, persona: str, extra: str = "") -> str:
    p = PERSONAS[persona]
    sub = billing.subscription(user["id"])
    biz = business_context(user["id"])
    now = tools.local_now()
    days = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    return "\n\n".join(x for x in [
        p["brief"],
        COMMON_RULES,
        f"الوقت الآن عند العميل: {days[now.weekday()]} {now.strftime('%Y-%m-%d %H:%M')} (توقيت {now.tzname()}).",
        f"اسم العميل: {user['name']}. باقته: {billing.PLANS[sub['plan']]['name_ar']}.",
        f"ملف النشاط:\n{biz}" if biz else "ملف النشاط فارغ: اسأل عن أهم المعلومات الناقصة واحفظها بأداة save_business_profile.",
        extra,
    ] if x)


def _as_blocks(content: str | list[dict]) -> list[dict]:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content}]


def _append_message(messages: list[dict], role: str, content: str | list[dict]) -> None:
    """Append a turn while coalescing adjacent same-role turns for the Messages API."""
    if messages and messages[-1]["role"] == role:
        previous = messages[-1]["content"]
        if isinstance(previous, str) and isinstance(content, str):
            messages[-1]["content"] = previous + "\n\n" + content
        else:
            messages[-1]["content"] = _as_blocks(previous) + _as_blocks(content)
    else:
        messages.append({"role": role, "content": content})


def _history(conversation_id: str) -> list[dict]:
    raw_rows = db.all_("SELECT user_id, role, content FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?", (conversation_id, HISTORY_MESSAGES))
    rows = list(reversed(raw_rows))
    # Include binary images/PDFs for the latest three attachment-bearing turns. This lets
    # follow-up questions refer to files without repeatedly sending the whole conversation's files.
    binary_turns: list[int] = []
    parsed: list[dict] = []
    for index, row in enumerate(rows):
        data = db.jload(row["content"], {"text": row["content"]})
        parsed.append(data if isinstance(data, dict) else {"text": str(data)})
        if row["role"] == "user" and any(a.get("kind") in ("image", "pdf") for a in parsed[-1].get("attachments", []) if isinstance(a, dict)):
            binary_turns.append(index)
    include_binary = set(binary_turns[-3:])
    out: list[dict] = []
    for index, row in enumerate(rows):
        data = parsed[index]
        text = str(data.get("text", ""))
        if row["role"] == "assistant" and data.get("actions"):
            text += "\n\n[إجراءات نُفذت في هذا الرد: " + json.dumps(data["actions"], ensure_ascii=False)[:3000] + "]"
        content: str | list[dict] = text
        file_list = [a for a in data.get("attachments", []) if isinstance(a, dict)]
        if row["role"] == "user" and file_list:
            blocks = [{"type": "text", "text": text or "يرجى فحص المرفقات ومساعدتي بناءً عليها."}]
            blocks.extend(attachment_service.model_blocks(row["user_id"], file_list, include_binary=index in include_binary))
            content = blocks
        if not text.strip() and not file_list:
            continue
        _append_message(out, row["role"], content)
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _summarize(result) -> object:
    text = json.dumps(result, ensure_ascii=False, default=str)
    return result if len(text) <= 1500 else text[:1500] + "…"


def run_turn(user: dict, conversation: dict, user_text: str, extra_system: str = "", attachments: list[dict] | None = None) -> Iterator[dict]:
    """Yield events: text / tool / done / error. Persists the exchange."""
    persona = conversation["persona"]
    try:
        billing.check(user["id"], "messages")
    except billing.LimitReached as exc:
        yield {"type": "error", "code": "limit_reached", "message": tools._err(exc)}
        return
    attachments = attachments or []
    history = _history(conversation["id"])
    db.insert("messages", {"id": db.new_id(), "conversation_id": conversation["id"], "user_id": user["id"], "role": "user",
                            "content": db.jdump({"text": user_text, "attachments": attachments}), "created_at": db.now()})
    if not conversation["title"]:
        title = user_text[:70] or ("مرفقات: " + ", ".join(a.get("filename", "ملف") for a in attachments)[:70])
        db.update("conversations", {"id": conversation["id"]}, {"title": title[:80]})
    messages = list(history)
    if attachments:
        current_content: str | list[dict] = [{"type": "text", "text": user_text or "يرجى فحص المرفقات ومساعدتي بناءً عليها."}]
        current_content.extend(attachment_service.model_blocks(user["id"], attachments, include_binary=True))
    else:
        current_content = user_text
    _append_message(messages, "user", current_content)
    if messages and messages[0]["role"] != "user":
        messages = messages[1:]
    system = system_prompt(user, persona, extra_system)
    tool_defs = tools.tools_for(persona)

    events: "queue.Queue[dict | None]" = queue.Queue()

    def worker() -> None:
        full_text: list[str] = []
        actions: list[dict] = []
        try:
            for _ in range(MAX_ROUNDS):
                rnd = llm.provider().stream_round(system, messages, tool_defs, lambda d: (full_text.append(d), events.put({"type": "text", "delta": d})))
                messages.append({"role": "assistant", "content": rnd["content"]})
                uses = [b for b in rnd["content"] if b.get("type") == "tool_use"]
                if rnd["stop_reason"] != "tool_use" or not uses:
                    break
                results = []
                for b in uses:
                    events.put({"type": "tool", "name": b["name"], "label": TOOL_LABELS.get(b["name"], b["name"]), "status": "running"})
                    try:
                        res = tools.run_tool(user, persona, b["name"], b.get("input") or {})
                        ok = True
                    except tools.ToolError as exc:
                        res, ok = {"error": str(exc)}, False
                    actions.append({"tool": b["name"], "ok": ok, "result": _summarize(res)})
                    events.put({"type": "tool", "name": b["name"], "label": TOOL_LABELS.get(b["name"], b["name"]), "status": "done" if ok else "failed",
                                "approval": isinstance(res, dict) and res.get("status") == "pending_client_approval", "result": _summarize(res)})
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(res, ensure_ascii=False, default=str)[:30000], "is_error": not ok})
                    if full_text and not full_text[-1].endswith("\n"):
                        full_text.append("\n\n")
                        events.put({"type": "text", "delta": "\n\n"})
                messages.append({"role": "user", "content": results})
            billing.consume(user["id"], "messages")
            mid = db.new_id()
            db.insert("messages", {"id": mid, "conversation_id": conversation["id"], "user_id": user["id"], "role": "assistant",
                                   "content": db.jdump({"text": "".join(full_text).strip(), "actions": actions}), "created_at": db.now()})
            db.update("conversations", {"id": conversation["id"]}, {"updated_at": db.now()})
            events.put({"type": "done", "message_id": mid})
        except llm.ProviderNotConfigured:
            events.put({"type": "error", "code": "not_configured", "message": "خدمة المحادثة لم تُفعَّل بعد على الخادم (مفتاح مزود النماذج غير مضبوط)."})
        except llm.ProviderError as exc:
            events.put({"type": "error", "code": "provider_error", "message": "صار خلل مؤقت عند مزود الخدمة، جرّب بعد قليل.", "detail": str(exc)[:300]})
        except Exception as exc:  # noqa: BLE001
            events.put({"type": "error", "code": "internal", "message": "صار خطأ غير متوقع.", "detail": str(exc)[:300]})
        finally:
            events.put(None)
            db.close_thread()

    threading.Thread(target=worker, daemon=True).start()
    while True:
        ev = events.get()
        if ev is None:
            return
        yield ev
