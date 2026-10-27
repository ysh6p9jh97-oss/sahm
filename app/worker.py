"""Background worker: publishes scheduled posts, runs scheduled automations,
sends task reminders and refreshes post insights.

Runs inside the web process by default (SAHM_RUN_WORKER=1). For larger
deployments run it as its own process:  python -m app.worker
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

from app import config, db, mailer
from app.services import automations, posts

log = logging.getLogger("sahm.worker")
TICK = 30


def publish_due() -> int:
    n = 0
    for p in db.all_("SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at LIMIT 20", (db.now(),)):
        posts.publish(p)
        n += 1
    return n


def send_reminders() -> int:
    """One email per user per day listing tasks due tomorrow (client timezone)."""
    if not mailer.configured():
        return 0
    zone = ZoneInfo(config.settings.timezone)
    now = dt.datetime.now(zone)
    if now.hour < 9:
        return 0
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    sent = 0
    rows = db.all_(
        "SELECT t.*, u.email, u.name AS user_name, p.name AS project_name FROM tasks t JOIN users u ON u.id = t.user_id "
        "JOIN projects p ON p.id = t.project_id WHERE t.due_date = ? AND t.status != 'done' AND t.reminded_at IS NULL", (tomorrow,))
    by_user: dict[str, list[dict]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)
    for user_id, tasks in by_user.items():
        lines = "\n".join(f"- {t['title']} ({t['project_name']})" for t in tasks)
        try:
            mailer.send(tasks[0]["email"], "تذكير: مهام موعدها بكرة — نجد | شهم", f"هلا {tasks[0]['user_name']}،\n\nهذي المهام موعدها بكرة {tomorrow}:\n{lines}\n\nنجد — فريق شهم")
            for t in tasks:
                db.update("tasks", {"id": t["id"]}, {"reminded_at": db.now()})
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("reminder email failed for %s: %s", user_id, exc)
    return sent


def refresh_insights() -> int:
    n = 0
    cutoff = db.now() - 14 * 86400
    stale = db.now() - 6 * 3600
    for p in db.all_("SELECT * FROM posts WHERE status IN ('published','partial') AND published_at >= ? AND (insights_at IS NULL OR insights_at < ?) LIMIT 20", (cutoff, stale)):
        try:
            posts.refresh_insights(p)
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("insights failed for %s: %s", p["id"], exc)
    return n


def housekeeping() -> None:
    db.run("DELETE FROM oauth_states WHERE created_at < ?", (db.now() - 3600,))
    db.run("UPDATE posts SET status = 'scheduled' WHERE status = 'publishing' AND updated_at < ?", (db.now() - 1800,))


def tick() -> dict:
    out = {}
    for name, fn in (("posts", publish_due), ("automations", automations.run_due), ("reminders", send_reminders), ("insights", refresh_insights)):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("worker step %s failed: %s", name, exc)
    housekeeping()
    return out


def loop(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    db.init()
    log.info("worker started")
    while not stop.is_set():
        tick()
        stop.wait(TICK)


def start_background() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=loop, args=(stop,), daemon=True, name="sahm-worker").start()
    return stop


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            loop()
        except Exception:  # noqa: BLE001
            log.exception("worker crashed; restarting in 10s")
            time.sleep(10)
