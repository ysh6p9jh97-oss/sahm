"""Local UI check server with a simulated model (not for production)."""
import os, sys, tempfile
os.environ.setdefault("SAHM_DATA_DIR", tempfile.mkdtemp(prefix="sahm-demo-"))
os.environ.setdefault("SAHM_RUN_WORKER", "0")
os.environ.setdefault("SAHM_BASE_URL", "http://127.0.0.1:8765")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, llm  # noqa
config.reload_settings()
from app.llm import Round  # noqa


class Demo:
    def stream_round(self, system, messages, tools, on_text, fast=False, max_tokens=4096):
        last = messages[-1]
        names = [t["name"] for t in tools]
        if isinstance(last["content"], list):  # after tool results
            txt = "تمام، نفّذت المطلوب وجهزته لك. كل شيء يحتاج موافقتك تلقاه في **بانتظار موافقتك**:\n- راجع التفاصيل\n- اعتمد بضغطة"
            on_text(txt); return Round(content=[{"type": "text", "text": txt}], stop_reason="end_turn")
        if "design_post_image" in names:
            return Round(content=[{"type": "tool_use", "id": "a1", "name": "design_post_image", "input": {"headline": "عطور نجد — إصدار الشتاء", "subline": "روائح دافئة تليق بليالي الرياض", "style": "bold"}}], stop_reason="tool_use")
        if "create_project_plan" in names:
            return Round(content=[{"type": "tool_use", "id": "a2", "name": "create_project_plan", "input": {"name": "إطلاق متجر عطور نجد", "goal": "إطلاق المتجر خلال ٣٠ يومًا", "tasks": [
                {"title": "تحديد ٨ منتجات للإطلاق", "assignee": "me", "due_date": "2026-11-02", "priority": "high"},
                {"title": "صفحة هبوط مع نموذج طلب", "assignee": "sari", "due_date": "2026-11-06"},
                {"title": "تقويم محتوى أسبوعين", "assignee": "sulaf", "due_date": "2026-11-08"},
                {"title": "بوت واتساب للرد على الاستفسارات", "assignee": "sanad", "due_date": "2026-11-10"}]}}], stop_reason="tool_use")
        if "create_site" in names:
            html = '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0;font-family:Tahoma,sans-serif;background:#120d1c;color:#fff}header{padding:80px 24px;text-align:center;background:radial-gradient(circle at 70% 0,#6d3df5,#120d1c 70%)}h1{font-size:40px;margin:0 0 12px}form{max-width:420px;margin:30px auto;display:grid;gap:10px;padding:0 24px}input,textarea{padding:12px;border-radius:10px;border:0}button{padding:12px;border:0;border-radius:10px;background:#6d3df5;color:#fff;font-size:16px}</style></head><body><header><h1>عطور نجد</h1><p>روائح شرقية فاخرة تصلك لباب بيتك</p></header><form method="POST" action="__FORM__"><input name="name" placeholder="الاسم"><input name="phone" placeholder="الجوال"><textarea name="message" placeholder="طلبك"></textarea><button>اطلب الآن</button></form></body></html>'
            return Round(content=[{"type": "tool_use", "id": "a3", "name": "create_site", "input": {"title": "Najd Perfumes", "html": html}}], stop_reason="tool_use")
        if "create_automation" in names:
            return Round(content=[{"type": "tool_use", "id": "a4", "name": "create_automation", "input": {"name": "تقرير الطلبات اليومي", "trigger": {"type": "schedule", "cron": "0 9 * * *"}, "steps": [{"type": "notify", "title": "صباح الخير", "body": "راجع طلبات اليوم"}]}}], stop_reason="tool_use")
        txt = "هلا! وش تحتاج؟"; on_text(txt)
        return Round(content=[{"type": "text", "text": txt}], stop_reason="end_turn")


llm.set_provider(Demo())
from app.web import build_app  # noqa
app = build_app(start_worker=False)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
