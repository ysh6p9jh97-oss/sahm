"""End-to-end tests for SAHM. Run:  python -m unittest discover -s tests -v

The model provider is replaced by a scripted fake, and outbound platform
calls (Telegram, WhatsApp, Moyasar) are patched, so tests need no network.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="sahm-test-")
os.environ.update({
    "SAHM_DATA_DIR": TMP, "SAHM_ENV": "test", "SAHM_BASE_URL": "http://testserver", "SAHM_RUN_WORKER": "0",
    "META_APP_SECRET": "meta-secret", "WHATSAPP_VERIFY_TOKEN": "verify-me", "MOYASAR_WEBHOOK_SECRET": "moy-secret",
    "MOYASAR_SECRET_KEY": "sk_test_x", "SMTP_HOST": "",
})

from starlette.testclient import TestClient  # noqa: E402

from app import billing, config, db, llm, security  # noqa: E402
from app.llm import Round  # noqa: E402

H = {"x-sahm": "1"}


class ScriptedProvider:
    """Plays back rounds: each item is either text or a list of tool calls."""

    def __init__(self):
        self.script: list = []
        self.calls: list = []

    def stream_round(self, system, messages, tools, on_text, fast=False, max_tokens=4096):
        self.calls.append({"system": system, "messages": [m for m in messages], "tools": [t["name"] for t in tools]})
        step = self.script.pop(0) if self.script else "تمام."
        if isinstance(step, str):
            on_text(step)
            return Round(content=[{"type": "text", "text": step}], stop_reason="end_turn")
        blocks = [{"type": "tool_use", "id": f"tu_{i}_{len(self.calls)}", "name": n, "input": a} for i, (n, a) in enumerate(step)]
        return Round(content=blocks, stop_reason="tool_use")


def setUpModule():
    config.reload_settings()
    db.reset_for_tests()


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        from app.web import build_app
        security.limiter.reset()
        self.fake = ScriptedProvider()
        llm.set_provider(self.fake)
        self.client = TestClient(build_app(start_worker=False))

    def register(self, client=None, email=None, name="عبير"):
        c = client or self.client
        email = email or f"u{db.new_id()[:8]}@example.com"
        r = c.post("/api/auth/register", json={"name": name, "email": email, "password": "password123"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def chat(self, persona, text, client=None):
        c = client or self.client
        conv = c.post("/api/conversations", json={"persona": persona}, headers=H).json()
        r = c.post(f"/api/conversations/{conv['id']}/messages", json={"text": text}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        return conv, events

    def add_telegram_channel(self, user_id, chat="-100123"):
        from app.services import channels as ch
        with mock.patch("app.billing.check"):
            return ch.upsert(user_id, "telegram", f"1:{chat}", "قناتي", "123:" + "a" * 30, {"chat_id": chat, "webhook_secret": "tgsecret", "bot_username": "b"})


class AuthTests(Base):
    def test_register_login_logout_and_csrf(self):
        u = self.register(email="a@example.com")
        self.assertEqual(u["plan"], "free")
        self.assertEqual(self.client.get("/api/me").json()["email"], "a@example.com")
        # writes without the CSRF header are rejected
        self.assertEqual(self.client.post("/api/projects", json={"name": "x"}).status_code, 403)
        self.client.post("/api/auth/logout", headers=H)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        r = self.client.post("/api/auth/login", json={"email": "a@example.com", "password": "password123"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/auth/register", json={"name": "x", "email": "a@example.com", "password": "password123"}, headers=H).status_code, 409)

    def test_login_throttle(self):
        self.register(email="t@example.com")
        c = TestClient(self.client.app)
        for _ in range(8):
            c.post("/api/auth/login", json={"email": "t@example.com", "password": "wrong-pass"}, headers=H)
        r = c.post("/api/auth/login", json={"email": "t@example.com", "password": "password123"}, headers=H)
        self.assertEqual(r.status_code, 429)

    def test_password_change_invalidates_other_sessions(self):
        self.register(email="p@example.com")
        other = TestClient(self.client.app)
        other.post("/api/auth/login", json={"email": "p@example.com", "password": "password123"}, headers=H)
        self.assertEqual(other.get("/api/me").status_code, 200)
        r = self.client.post("/api/me/password", json={"current": "password123", "new": "newpassword1"}, headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(other.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_password_hashing(self):
        h = security.hash_password("secret-pass")
        self.assertTrue(security.verify_password("secret-pass", h))
        self.assertFalse(security.verify_password("wrong", h))


class IsolationTests(Base):
    def test_users_cannot_see_each_other(self):
        a = TestClient(self.client.app)
        b = TestClient(self.client.app)
        self.register(a)
        self.register(b)
        p = a.post("/api/projects", json={"name": "سري"}, headers=H).json()
        self.assertEqual(b.get(f"/api/projects/{p['id']}").status_code, 404)
        self.assertEqual(b.get("/api/projects").json(), [])
        conv = a.post("/api/conversations", json={"persona": "najd"}, headers=H).json()
        self.assertEqual(b.get(f"/api/conversations/{conv['id']}").status_code, 404)
        self.assertEqual(b.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"}, headers=H).status_code, 404)


class ChatAttachmentTests(Base):
    def test_chat_upload_is_private_and_passed_to_the_persona(self):
        self.register()
        conv = self.client.post("/api/conversations", json={"persona": "sulaf"}, headers=H).json()
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (24, 24), color=(255, 0, 0)).save(buf, format="PNG")
        self.fake.script = ["وصلتني الصورة."]
        response = self.client.post(
            f"/api/conversations/{conv['id']}/messages",
            data={"text": "حللي هذه الصورة"},
            files={"files": ("brief.png", buf.getvalue(), "image/png")},
            headers=H,
        )
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        self.assertEqual(events[-1]["type"], "done")
        model_messages = self.fake.calls[0]["messages"]
        user_content = model_messages[-1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertIn("image", [block.get("type") for block in user_content])
        saved = self.client.get(f"/api/conversations/{conv['id']}").json()["messages"][0]
        self.assertEqual(saved["attachments"][0]["filename"], "brief.png")
        file_url = saved["attachments"][0]["url"]
        file_response = self.client.get(file_url)
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.headers["content-type"], "image/jpeg")
        other = TestClient(self.client.app)
        self.assertEqual(other.get(file_url).status_code, 401)


class NajdTests(Base):
    def test_najd_creates_a_real_project_plan(self):
        self.register()
        self.fake.script = [
            [("create_project_plan", {"name": "إطلاق متجر العطور", "goal": "إطلاق خلال شهر", "tasks": [
                {"title": "تحديد المنتجات", "assignee": "me", "due_date": "2026-11-05", "priority": "high"},
                {"title": "صفحة هبوط", "assignee": "sari", "due_date": "2026-11-10"},
                {"title": "خطة محتوى", "assignee": "sulaf", "due_date": "2026-11-12"},
            ]})],
            "جهزت لك الخطة بثلاث مهام.",
        ]
        conv, events = self.chat("najd", "أبي خطة لإطلاق متجر عطور")
        kinds = [e["type"] for e in events]
        self.assertIn("tool", kinds)
        self.assertEqual(kinds[-1], "done")
        projects_ = self.client.get("/api/projects").json()
        self.assertEqual(projects_[0]["name"], "إطلاق متجر العطور")
        self.assertEqual(projects_[0]["total"], 3)
        detail = self.client.get(f"/api/projects/{projects_[0]['id']}").json()
        self.assertEqual({t["assignee"] for t in detail["tasks"]}, {"me", "sari", "sulaf"})
        # conversation persisted with the tool action
        msgs = self.client.get(f"/api/conversations/{conv['id']}").json()["messages"]
        self.assertEqual(msgs[-1]["actions"][0]["tool"], "create_project_plan")
        # system prompt carries persona + rules + local time
        self.assertIn("نجد", self.fake.calls[0]["system"])
        self.assertIn("create_project_plan", self.fake.calls[0]["tools"])
        self.assertNotIn("create_site", self.fake.calls[0]["tools"])

    def test_calendar_feed(self):
        self.register()
        p = self.client.post("/api/projects", json={"name": "مشروع"}, headers=H).json()
        self.client.post(f"/api/projects/{p['id']}/tasks", json={"title": "اجتماع", "due_date": "2026-12-01"}, headers=H)
        url = self.client.get(f"/api/projects/{p['id']}").json()["calendar_url"].replace("http://testserver", "")
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn("DTSTART;VALUE=DATE:20261201", r.text)
        self.assertEqual(self.client.get(url.replace(url.split("/")[-1], "bad.ics")).status_code, 404)

    def test_task_status_flow(self):
        self.register()
        p = self.client.post("/api/projects", json={"name": "م"}, headers=H).json()
        t = self.client.post(f"/api/projects/{p['id']}/tasks", json={"title": "مهمة"}, headers=H).json()
        r = self.client.patch(f"/api/tasks/{t['id']}", json={"status": "done"}, headers=H).json()
        self.assertEqual(r["status"], "done")
        r = self.client.patch(f"/api/tasks/{t['id']}", json={"status": "nonsense"}, headers=H).json()
        self.assertEqual(r["status"], "done")


class SulafTests(Base):
    def test_design_and_post_requires_approval_then_publishes(self):
        u = self.register()
        chan = self.add_telegram_channel(u["id"])
        self.fake.script = [
            [("design_post_image", {"headline": "خصم ٢٠٪ على كل العطور", "subline": "لمدة ٣ أيام فقط", "style": "offer"})],
            "صممت المنشور.",
        ]
        self.chat("sulaf", "صممي منشور عرض")
        media_rows = self.client.get("/api/media").json()
        self.assertEqual(len(media_rows), 1)
        img = self.client.get(media_rows[0]["url"].replace("http://testserver", ""))
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img.headers["content-type"], "image/jpeg")

        self.fake.script = [[("create_post", {"caption": "عرض الأسبوع 🌟", "channel_ids": [chan["id"]], "media_ids": [media_rows[0]["id"]]})], "جاهز للاعتماد."]
        self.chat("sulaf", "انشريه على تيليجرام")
        posts_ = self.client.get("/api/posts").json()
        self.assertEqual(posts_[0]["status"], "pending_approval")
        approvals = self.client.get("/api/approvals").json()
        self.assertEqual(approvals[0]["action"], "publish_post")

        with mock.patch("app.channels.telegram.send_media", return_value={"id": "777"}) as send:
            r = self.client.post(f"/api/approvals/{approvals[0]['id']}/approve", headers=H).json()
        self.assertEqual(r["status"], "approved")
        send.assert_called_once()
        p = self.client.get("/api/posts").json()[0]
        self.assertEqual(p["status"], "published")
        self.assertEqual(p["results"][chan["id"]]["id"], "777")
        self.assertEqual(billing.used(u["id"], "posts"), 1)

    def test_instagram_requires_media_and_errors_are_reported_to_model(self):
        u = self.register()
        from app.services import channels as ch
        ig = ch.upsert(u["id"], "instagram", "1789", "@shop", "tok", {"username": "shop"})
        self.fake.script = [[("create_post", {"caption": "نص فقط", "channel_ids": [ig["id"]]})], "محتاجين صورة."]
        _, events = self.chat("sulaf", "انشري")
        tool_ev = [e for e in events if e["type"] == "tool" and e["status"] == "failed"]
        self.assertTrue(tool_ev)
        self.assertIn("Instagram", json.dumps(tool_ev[0]["result"]))
        last_round = self.fake.calls[-1]["messages"][-1]["content"][0]
        self.assertTrue(last_round["is_error"])

    def test_scheduled_post_is_published_by_worker(self):
        u = self.register()
        chan = self.add_telegram_channel(u["id"])
        from app import worker
        from app.services import posts
        p = posts.create(u["id"], "مجدول", [], [chan["id"]], db.now() - 5)
        posts.approve(u["id"], p["id"])
        with mock.patch("app.channels.telegram.send_message", return_value={"id": "9"}):
            self.assertEqual(worker.publish_due(), 1)
        self.assertEqual(posts.get(u["id"], p["id"])["status"], "published")
        with mock.patch("app.channels.telegram.send_message") as again:
            self.assertEqual(worker.publish_due(), 0)
            again.assert_not_called()

    def test_free_plan_post_limit(self):
        u = self.register()
        chan = self.add_telegram_channel(u["id"])
        from app.services import posts
        billing.consume(u["id"], "posts", 4)
        p = posts.create(u["id"], "x", [], [chan["id"]], None)
        posts.approve(u["id"], p["id"])
        posts.publish(posts.get(u["id"], p["id"]))
        self.assertEqual(posts.get(u["id"], p["id"])["status"], "failed")


class SariTests(Base):
    def test_site_build_publish_form_and_sandbox(self):
        u = self.register()
        html = '<!doctype html><html lang="ar" dir="rtl"><body><h1>عطور نجد</h1><form method="POST" action="__FORM__"><input name="name"></form></body></html>'
        self.fake.script = [[("create_site", {"title": "Najd Perfumes", "html": html})], [("request_publish_site", {"site_id": "PLACEHOLDER"})], "تم."]

        def fill(*a, **k):
            # replace placeholder with the real id after creation
            for item in self.fake.script:
                if isinstance(item, list):
                    for _, args in item:
                        row = db.one("SELECT id FROM sites WHERE user_id = ?", (u["id"],))
                        if args.get("site_id") == "PLACEHOLDER" and row:
                            args["site_id"] = row["id"]
        orig = self.fake.stream_round

        def wrapped(*a, **k):
            fill()
            return orig(*a, **k)
        self.fake.stream_round = wrapped
        self.chat("sari", "أبي صفحة هبوط")
        site = self.client.get("/api/sites").json()[0]
        self.assertEqual(site["status"], "draft")
        full = self.client.get(f"/api/sites/{site['id']}").json()
        self.assertIn(f"/f/{site['slug']}", full["html"])
        self.assertEqual(self.client.get(f"/s/{site['slug']}").status_code, 404)  # not public before approval
        ap = self.client.get("/api/approvals").json()[0]
        self.assertEqual(ap["action"], "publish_site")
        self.client.post(f"/api/approvals/{ap['id']}/approve", headers=H)
        pub = self.client.get(f"/s/{site['slug']}")
        self.assertEqual(pub.status_code, 200)
        self.assertIn("sandbox", pub.headers["content-security-policy"])
        # a visitor submits the form → contact + notification
        anon = TestClient(self.client.app)
        r = anon.post(f"/f/{site['slug']}", data={"name": "سارة", "phone": "0551234567", "message": "أبي أطلب"})
        self.assertEqual(r.status_code, 200)
        contacts = self.client.get("/api/contacts").json()
        self.assertEqual(contacts[0]["name"], "سارة")
        self.assertEqual(contacts[0]["phone"], "0551234567")
        z = self.client.get(f"/api/sites/{site['id']}/zip")
        self.assertEqual(z.headers["content-type"], "application/zip")


class SanadTests(Base):
    def test_cron(self):
        from app.services.automations import AutomationError, next_run, parse_cron
        import datetime as dt
        from zoneinfo import ZoneInfo
        base = int(dt.datetime(2026, 10, 26, 8, 0, tzinfo=ZoneInfo("Asia/Riyadh")).timestamp())  # Monday
        n = next_run("0 9 * * *", base)
        self.assertEqual(dt.datetime.fromtimestamp(n, ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M"), "2026-10-26 09:00")
        n = next_run("30 7 * * 0", base)  # next Sunday
        self.assertEqual(dt.datetime.fromtimestamp(n, ZoneInfo("Asia/Riyadh")).strftime("%a %H:%M"), "Sun 07:30")
        with self.assertRaises(AutomationError):
            parse_cron("61 * * * *")

    def test_scheduled_automation_lifecycle(self):
        u = self.register()
        self.fake.script = [[("create_automation", {"name": "تذكير يومي", "trigger": {"type": "schedule", "cron": "0 9 * * *"},
                                                    "steps": [{"type": "notify", "title": "صباح الخير", "body": "راجع طلبات اليوم {{now}}"}]})], "جاهزة."]
        self.chat("sanad", "أبي تذكير كل يوم 9")
        autos = self.client.get("/api/automations").json()
        self.assertEqual(autos[0]["status"], "draft")
        ap = self.client.get("/api/approvals").json()[0]
        self.assertEqual(ap["action"], "activate_automation")
        self.client.post(f"/api/approvals/{ap['id']}/approve", headers=H)
        a = self.client.get(f"/api/automations/{autos[0]['id']}").json()
        self.assertEqual(a["status"], "active")
        self.assertIsNotNone(a["next_run_at"])
        db.run("UPDATE automations SET next_run_at = ? WHERE id = ?", (db.now() - 1, a["id"]))
        from app.services import automations
        self.assertEqual(automations.run_due(), 1)
        a = self.client.get(f"/api/automations/{a['id']}").json()
        self.assertEqual(a["runs"][0]["status"], "succeeded")
        self.assertGreater(a["next_run_at"], db.now())
        notes = self.client.get("/api/notifications").json()["items"]
        self.assertTrue(any(n["title"] == "صباح الخير" for n in notes))

    def test_webhook_automation_and_ssrf_guard(self):
        u = self.register()
        from app.services import automations
        a = automations.create(u["id"], "طلب متجر", "", {"type": "webhook"}, [{"type": "create_task", "title": "طلب جديد: {{trigger.body.order}}"}])
        self.assertEqual(TestClient(self.client.app).post(f"/hooks/{a['webhook_token']}", json={"order": "A1"}).status_code, 404)  # inactive
        automations.activate(u["id"], a["id"])
        r = TestClient(self.client.app).post(f"/hooks/{a['webhook_token']}", json={"order": "A1"})
        self.assertEqual(r.json()["status"], "succeeded")
        titles = [t["title"] for t in db.all_("SELECT title FROM tasks WHERE user_id = ?", (u["id"],))]
        self.assertIn("طلب جديد: A1", titles)
        with self.assertRaises(automations.AutomationError):
            automations.create(u["id"], "x", "", {"type": "manual"}, [{"type": "http_request", "url": "http://127.0.0.1:8000/admin"}])

    def test_whatsapp_bot_replies_from_business_profile(self):
        u = self.register()
        self.client.put("/api/profile", json={"business_name": "عطور نجد", "faq": "الدوام من 4 العصر إلى 12 الليل"}, headers=H)
        from app.services import automations, channels as ch
        with mock.patch("app.billing.check"):
            wa = ch.upsert(u["id"], "whatsapp", "555000", "عطور نجد", "tok" * 10, {"display_phone": "+966500000000"})
        billing.activate_plan(u["id"], "pro")
        a = automations.create(u["id"], "بوت واتساب", "", {"type": "whatsapp_inbound", "channel_id": wa["id"]}, [{"type": "ai_reply"}, {"type": "reply"}])
        automations.activate(u["id"], a["id"])
        payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "555000"}, "contacts": [{"wa_id": "966511111111", "profile": {"name": "خالد"}}],
                                                      "messages": [{"from": "966511111111", "id": "wamid.1", "type": "text", "text": {"body": "متى تفتحون؟"}}]}}]}]}
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"meta-secret", raw, hashlib.sha256).hexdigest()
        self.fake.script = ["نفتح من ٤ العصر إلى ١٢ الليل، حياك."]
        anon = TestClient(self.client.app)
        with mock.patch("app.channels.whatsapp.send_text", return_value={"id": "wamid.out"}) as send:
            self.assertEqual(anon.post("/hooks/whatsapp", content=raw, headers={"x-hub-signature-256": "sha256=bad", "content-type": "application/json"}).status_code, 401)
            r = anon.post("/hooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig, "content-type": "application/json"})
            self.assertEqual(r.status_code, 200)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], "966511111111")
            self.assertIn("٤ العصر", send.call_args.args[3])
            # duplicate delivery is ignored
            anon.post("/hooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig, "content-type": "application/json"})
            send.assert_called_once()
        self.assertIn("الدوام", self.fake.calls[-1]["messages"][0]["content"])
        self.assertEqual(billing.used(u["id"], "whatsapp_sends"), 1)

    def test_whatsapp_verification_handshake(self):
        r = self.client.get("/hooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
        self.assertEqual(r.text, "42")
        self.assertEqual(self.client.get("/hooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code, 403)


class CoreAndBillingTests(Base):
    def test_routing_keywords(self):
        from app import core
        self.assertEqual(core.route("أبي موقع لمتجري")["persona"], "sari")
        self.assertEqual(core.route("أبي بوت واتساب يرد")["persona"], "sanad")
        self.assertEqual(core.route("سوي لي حملة إعلان انستقرام")["persona"], "sulaf")

    def test_team_mode_creates_assigned_tasks(self):
        self.register()
        self.fake.script = [json.dumps({"name": "متجر", "tasks": [
            {"title": "خطة محتوى", "assignee": "sulaf", "details": "١٢ منشور"}, {"title": "صفحة هبوط", "assignee": "sari"},
            {"title": "بوت", "assignee": "sanad"}, {"title": "ربط حساب إنستقرام", "assignee": "me"}]}, ensure_ascii=False)]
        r = self.client.post("/api/core/team", json={"goal": "أبي أطلق متجر إلكتروني"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        p = self.client.get(f"/api/projects/{r.json()['project_id']}").json()
        self.assertEqual([t["assignee"] for t in p["tasks"]], ["sulaf", "sari", "sanad", "me"])

    def test_message_limit(self):
        u = self.register()
        billing.consume(u["id"], "messages", 40)
        _, events = self.chat("najd", "hi")
        self.assertEqual(events[-1]["code"], "limit_reached")

    def test_moyasar_webhook_settles_only_verified_paid_invoices(self):
        u = self.register()
        db.insert("invoices", {"id": "inv1", "user_id": u["id"], "provider_id": "moy_1", "plan": "pro", "amount_halalas": 14900, "status": "initiated", "url": "x", "created_at": db.now(), "paid_at": None})
        anon = TestClient(self.client.app)
        evt = {"type": "payment_paid", "secret_token": "wrong", "data": {"invoice_id": "moy_1"}}
        self.assertEqual(anon.post("/api/billing/webhook", json=evt).status_code, 403)
        evt["secret_token"] = "moy-secret"
        with mock.patch("app.billing.fetch_moyasar_invoice", return_value={"status": "initiated", "amount": 14900}):
            anon.post("/api/billing/webhook", json=evt)
        self.assertEqual(billing.subscription(u["id"])["plan"], "free")
        with mock.patch("app.billing.fetch_moyasar_invoice", return_value={"status": "paid", "amount": 14900}):
            anon.post("/api/billing/webhook", json=evt)
            anon.post("/api/billing/webhook", json=evt)  # idempotent
        sub = billing.subscription(u["id"])
        self.assertEqual(sub["plan"], "pro")
        self.assertLess(sub["current_period_end"] - db.now(), 31 * 86400)

    def test_expired_subscription_falls_back_to_free(self):
        u = self.register()
        billing.activate_plan(u["id"], "pro")
        db.run("UPDATE subscriptions SET current_period_end = ? WHERE user_id = ?", (db.now() - 10, u["id"]))
        self.assertEqual(billing.subscription(u["id"])["plan"], "free")

    def test_provider_not_configured_message(self):
        self.register()
        llm.set_provider(llm.AnthropicProvider())
        with mock.patch.object(config.settings, "anthropic_api_key", ""):
            _, events = self.chat("sari", "هلا")
        self.assertEqual(events[-1]["code"], "not_configured")

    def test_stream_parser(self):
        lines = [
            'event: content_block_start', 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"هلا"}}',
            'data: {"type":"content_block_stop","index":0}',
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"list_sites","input":{}}}',
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":"}}',
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"1}"}}',
            'data: {"type":"content_block_stop","index":1}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        got = []
        r = llm._parse_stream(iter(lines), got.append)
        self.assertEqual("".join(got), "هلا")
        self.assertEqual(r["stop_reason"], "tool_use")
        self.assertEqual(r["content"][1]["input"], {"a": 1})


class ConfigTests(Base):
    def test_production_requires_secrets(self):
        with mock.patch.dict(os.environ, {"SAHM_ENV": "production", "SAHM_SECRET_KEY": "", "SAHM_ENCRYPTION_KEY": "", "SAHM_BASE_URL": "http://x"}):
            problems = config.Settings().validate()
        self.assertEqual(len(problems), 3)

    def test_sites_served_on_separate_host(self):
        u = self.register()
        from app.services import sites
        s = sites.create(u["id"], "Shop", "<html><body>hi shop</body></html>")
        sites.publish(u["id"], s["id"])
        with mock.patch.object(config.settings, "sites_base_url", "https://sites.example.com"):
            r = TestClient(self.client.app).get(f"/{s['slug']}", headers={"host": "sites.example.com"})
            self.assertIn("hi shop", r.text)
            self.assertIn("sandbox", r.headers["content-security-policy"])
            r = TestClient(self.client.app).get("/api/me", headers={"host": "sites.example.com"})
            self.assertNotEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
