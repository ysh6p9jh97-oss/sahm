"""SAHM CORE — routes requests to the right persona and runs team workflows.

Team mode: a goal becomes a project whose tasks are assigned to personas.
Running a task starts that persona on it (with real tools) in the background;
its answer is stored on the task and the task moves to 'review'.
"""
from __future__ import annotations

import re
import threading

from app import db, llm, tools
from app.agent import run_turn
from app.personas import PERSONAS
from app.services import projects

KEYWORDS = {
    "sulaf": r"تسويق|إعلان|اعلان|محتوى|منشور|انستقرام|إنستقرام|تيك ?توك|سناب|حملة|متابعين|هاشتاق|بوست|marketing|post|instagram|tiktok|ads?",
    "najd": r"خطة|مشروع|مهام|مهمة|جدول|موعد|أولويات|تقرير|متابعة|plan|project|task|deadline",
    "sari": r"موقع|صفحة|برمج|كود|تطبيق|خطأ|api|website|landing|code|bug|app",
    "sanad": r"أتمت|اتمت|بوت|ربط|واتس|تيليجرام|تلقائي|n8n|webhook|automation|bot|integrat",
}


def route(text: str) -> dict:
    scores = {p: len(re.findall(rx, text, re.I)) for p, rx in KEYWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return {"persona": best, "reason": "keywords"}
    try:
        r = llm.complete_json(
            "Pick the best SAHM teammate for this request. sulaf=marketing & social accounts, najd=project management, "
            "sari=websites & code, sanad=automation, bots, integrations & tech support.\n"
            f'Request: """{text[:2000]}"""\nReturn {{"persona": "sulaf|najd|sari|sanad", "reason": "short Arabic reason"}}',
            fast=True, max_tokens=200,
        )
        if r.get("persona") in PERSONAS:
            return r
    except (llm.ProviderError, llm.ProviderNotConfigured, ValueError):
        pass
    return {"persona": "najd", "reason": "default"}


def team_plan(user: dict, goal: str) -> dict:
    today = tools.local_now().strftime("%Y-%m-%d")
    plan = llm.complete_json(
        "أنت منسق فريق شهم. حوّل هدف العميل إلى مشروع ومهام موزعة على الفريق:\n"
        "sulaf: التسويق وإدارة الحسابات والمحتوى. najd: التخطيط والمتابعة. sari: المواقع والبرمجة. sanad: الأتمتة والبوتات والربط. me: مهام يجب أن ينفذها العميل بنفسه (مثل ربط حساب أو توفير صور).\n"
        f"التاريخ اليوم {today}. المهام قصيرة وقابلة للتنفيذ، مرتبة، بمواعيد واقعية.\n"
        f'الهدف: """{goal[:3000]}"""\n'
        'أعد JSON: {"name": "...", "description": "...", "tasks": [{"title": "...", "details": "تعليمات واضحة للمنفذ", "assignee": "sulaf|najd|sari|sanad|me", "due_date": "YYYY-MM-DD", "priority": "low|medium|high"}]}',
        system="أنت منسق مشاريع دقيق. أعد JSON فقط.", fast=False, max_tokens=3000,
    )
    project = projects.create_project(user["id"], plan.get("name") or goal[:60], plan.get("description", ""), goal)
    count = 0
    for t in (plan.get("tasks") or [])[:40]:
        projects.add_task(user["id"], project["id"], t.get("title", ""), t.get("details", ""), t.get("assignee", "me"), t.get("due_date"), t.get("priority", "medium"))
        count += 1
    db.log_activity(user["id"], "core", f"وضع الفريق: {project['name']}", project["id"])
    return {"project_id": project["id"], "tasks": count}


def run_task(user: dict, task_id: str) -> dict:
    task = projects.get_task(user["id"], task_id)
    if not task:
        raise ValueError("task_not_found")
    if task["assignee"] not in PERSONAS:
        raise ValueError("this task is for the client, not a teammate")
    if task["status"] == "in_progress":
        raise ValueError("task already running")
    project = projects.get(user["id"], task["project_id"])
    projects.update_task(user["id"], task_id, status="in_progress")
    conv = {"id": db.new_id(), "user_id": user["id"], "persona": task["assignee"], "title": f"مهمة: {task['title']}"[:80], "created_at": db.now(), "updated_at": db.now()}
    db.insert("conversations", conv)
    prompt = (
        f"مهمة من مشروع «{project['name']}» (الهدف: {project['goal'] or project['description']}).\n"
        f"المهمة: {task['title']}\nالتفاصيل: {task['details'] or '—'}\n\n"
        "نفّذ المهمة بأدواتك قدر الإمكان الآن. إذا احتجت موافقة العميل فجهّز كل شيء واطلبها. "
        "اختم بملخص قصير لما تم وما ينتظر العميل."
    )

    def go() -> None:
        text, error = [], None
        for ev in run_turn(user, conv, prompt, extra_system="أنت تعمل ضمن وضع الفريق؛ العميل سيقرأ ملخصك لاحقًا في صفحة المشروع."):
            if ev["type"] == "text":
                text.append(ev["delta"])
            elif ev["type"] == "error":
                error = ev["message"]
        out = "".join(text).strip() or (error or "")
        projects.update_task(user["id"], task_id, status="blocked" if error and not text else "review", output=out + f"\n\n[المحادثة: #/chat/{task['assignee']}/{conv['id']}]")
        db.notify(user["id"], f"{PERSONAS[task['assignee']]['name']} أنهت مهمة" if task["assignee"] in ("sulaf", "najd") else f"{PERSONAS[task['assignee']]['name']} أنهى مهمة", task["title"], f"#/projects/{project['id']}")
        db.close_thread()

    threading.Thread(target=go, daemon=True).start()
    return {"task_id": task_id, "status": "in_progress", "conversation_id": conv["id"]}
