"""Najd's project & task management."""
from __future__ import annotations

import datetime as dt
import re

from app import billing, db

STATUSES = ("new", "planning", "in_progress", "review", "done", "blocked")
STATUS_AR = {"new": "جديدة", "planning": "قيد التخطيط", "in_progress": "قيد التنفيذ", "review": "بانتظار المراجعة", "done": "مكتملة", "blocked": "متعثرة"}
PRIORITIES = ("low", "medium", "high")
ASSIGNEES = ("sulaf", "najd", "sari", "sanad", "me")


def _date(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return None
    return value


def create_project(user_id: str, name: str, description: str = "", goal: str = "") -> dict:
    billing.check(user_id, "projects")
    row = {"id": db.new_id(), "user_id": user_id, "name": name.strip()[:160] or "مشروع جديد", "description": description[:4000], "goal": goal[:1000], "status": "active", "created_at": db.now(), "updated_at": db.now()}
    db.insert("projects", row)
    db.log_activity(user_id, "project", f"إنشاء مشروع: {row['name']}", row["id"])
    return row


def inbox(user_id: str) -> dict:
    """A default project that collects tasks created by automations."""
    p = db.one("SELECT * FROM projects WHERE user_id = ? AND name = 'الوارد'", (user_id,))
    if p:
        return p
    row = {"id": db.new_id(), "user_id": user_id, "name": "الوارد", "description": "مهام تصل من الأتمتة والنماذج", "goal": "", "status": "active", "created_at": db.now(), "updated_at": db.now()}
    db.insert("projects", row)
    return row


def get(user_id: str, project_id: str) -> dict | None:
    return db.one("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))


def list_projects(user_id: str) -> list[dict]:
    rows = db.all_("SELECT * FROM projects WHERE user_id = ? AND status != 'archived' ORDER BY updated_at DESC", (user_id,))
    for r in rows:
        stats = db.all_("SELECT status, COUNT(*) n FROM tasks WHERE project_id = ? GROUP BY status", (r["id"],))
        r["counts"] = {s["status"]: s["n"] for s in stats}
        r["total"] = sum(r["counts"].values())
    return rows


def add_task(user_id: str, project_id: str, title: str, details: str = "", assignee: str = "najd", due_date: str | None = None, priority: str = "medium") -> dict:
    if not get(user_id, project_id):
        raise ValueError("project_not_found")
    pos = db.one("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM tasks WHERE project_id = ?", (project_id,))["p"]
    row = {
        "id": db.new_id(), "user_id": user_id, "project_id": project_id, "title": title.strip()[:300] or "مهمة",
        "details": (details or "")[:4000], "assignee": assignee if assignee in ASSIGNEES else "me",
        "status": "new", "priority": priority if priority in PRIORITIES else "medium", "due_date": _date(due_date),
        "output": "", "reminded_at": None, "position": pos, "created_at": db.now(), "updated_at": db.now(),
    }
    db.insert("tasks", row)
    db.run("UPDATE projects SET updated_at = ? WHERE id = ?", (db.now(), project_id))
    return row


def get_task(user_id: str, task_id: str) -> dict | None:
    return db.one("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))


def update_task(user_id: str, task_id: str, **fields) -> dict:
    task = get_task(user_id, task_id)
    if not task:
        raise ValueError("task_not_found")
    allowed: dict = {}
    if "status" in fields and fields["status"] in STATUSES:
        allowed["status"] = fields["status"]
    if "title" in fields and fields["title"]:
        allowed["title"] = str(fields["title"])[:300]
    if "details" in fields and fields["details"] is not None:
        allowed["details"] = str(fields["details"])[:4000]
    if "assignee" in fields and fields["assignee"] in ASSIGNEES:
        allowed["assignee"] = fields["assignee"]
    if "priority" in fields and fields["priority"] in PRIORITIES:
        allowed["priority"] = fields["priority"]
    if "due_date" in fields:
        allowed["due_date"] = _date(fields["due_date"])
        allowed["reminded_at"] = None
    if "output" in fields and fields["output"] is not None:
        allowed["output"] = str(fields["output"])[:20000]
    if allowed:
        allowed["updated_at"] = db.now()
        db.update("tasks", {"id": task_id}, allowed)
        if allowed.get("status") == "done":
            db.log_activity(user_id, "task", f"إنجاز مهمة: {task['title']}", task_id)
    return get_task(user_id, task_id)


def tasks_for(user_id: str, project_id: str) -> list[dict]:
    return db.all_("SELECT * FROM tasks WHERE project_id = ? AND user_id = ? ORDER BY position", (project_id, user_id))


def ics(user_id: str, project_id: str | None = None) -> str:
    """iCalendar feed of dated tasks (importable into Google/Apple/Outlook calendars)."""
    q = "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.user_id = ? AND t.due_date IS NOT NULL"
    params: list = [user_id]
    if project_id:
        q += " AND t.project_id = ?"
        params.append(project_id)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SAHM//Najd//AR", "CALSCALE:GREGORIAN"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for t in db.all_(q, params):
        d = t["due_date"].replace("-", "")
        nxt = (dt.date.fromisoformat(t["due_date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
        summary = _ics_escape(f"{t['title']} — {t['project_name']}")
        lines += ["BEGIN:VEVENT", f"UID:{t['id']}@sahm", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{nxt}",
                  f"SUMMARY:{summary}", f"DESCRIPTION:{_ics_escape(STATUS_AR.get(t['status'], t['status']) + ' · ' + t['details'][:500])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def report(user_id: str) -> str:
    """Plain-text weekly report used by Najd's email."""
    today = dt.date.today()
    week_ago = db.now() - 7 * 86400
    lines = []
    for p in list_projects(user_id):
        tasks = tasks_for(user_id, p["id"])
        if not tasks:
            continue
        done_week = [t for t in tasks if t["status"] == "done" and t["updated_at"] >= week_ago]
        overdue = [t for t in tasks if t["status"] != "done" and t["due_date"] and dt.date.fromisoformat(t["due_date"]) < today]
        upcoming = [t for t in tasks if t["status"] != "done" and t["due_date"] and 0 <= (dt.date.fromisoformat(t["due_date"]) - today).days <= 7]
        total = len(tasks)
        done = len([t for t in tasks if t["status"] == "done"])
        lines.append(f"■ {p['name']} — الإنجاز {round(100 * done / total)}% ({done}/{total})")
        for t in done_week:
            lines.append(f"   ✓ أُنجزت: {t['title']}")
        for t in overdue:
            lines.append(f"   ! متأخرة: {t['title']} (كان موعدها {t['due_date']})")
        for t in upcoming:
            lines.append(f"   → قادمة: {t['title']} ({t['due_date']})")
    return "\n".join(lines)
