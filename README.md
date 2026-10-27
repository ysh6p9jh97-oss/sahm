# SAHM | شهم

**منصة عالمية تخدمك وتسندك** — a Saudi-rooted platform where four digital teammates execute real work for each client:

| Teammate | Executes |
|---|---|
| **Sulaf · سلاف** — Marketing & account management | Designs branded post images, publishes & schedules to Instagram, Facebook Pages, TikTok and Telegram channels, reads stats and comments, proposes comment replies, runs WhatsApp template campaigns to opted-in contacts |
| **Najd · نجد** — Projects & operations | Turns goals into projects with dated, prioritized tasks; assigns tasks to teammates; email reminders, weekly reports, calendar (ICS) feed |
| **Sari · ساري** — Engineering | Builds complete websites/landing pages, publishes them on a public link, contact forms that deliver leads, code download (zip), code review |
| **Sanad · سند** — Automation & support | Real automations: scheduled (cron), webhook, WhatsApp/Telegram inbound bots that answer from the business profile, site-form triggers; email/WhatsApp/Telegram/HTTP/task steps; run logs; export to n8n |

**SAHM CORE** routes requests and runs *team mode*: one goal → a project whose tasks go to the right teammate, who executes it with real tools.

**Safety model:** teammates may create drafts freely; anything that leaves SAHM (publishing, replying, messaging, activating, going live) becomes an **approval** the client must accept.

## Stack

One deployable service: Python **Starlette** (ASGI) + **SQLite** (WAL) + a no-build web app (vanilla JS, RTL/LTR). Background worker runs in-process (or `python -m app.worker`). Model provider: Anthropic Messages API (streaming + tool use), swappable in `app/llm.py`.

```
app/
  web.py            HTTP API, OAuth callbacks, webhooks, hosted sites, pages
  agent.py          persona loop: model ↔ tools, streamed as NDJSON events
  personas.py       identities, voice, service catalogues, instructions
  tools.py          tools per persona + approval actions
  core.py           routing + team mode
  services/         posts, projects, sites, automations, channels
  channels/         meta (IG/FB), tiktok, telegram, whatsapp, n8n
  billing.py        plans, limits, Moyasar invoices + webhook
  media.py          uploads + branded post designs (Pillow + raqm)
  worker.py         scheduled posts, automations, reminders, insights
web/                landing page, app, privacy, terms
tests/              end-to-end tests (simulated model, no network)
```

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # set SAHM_ENV=development, ANTHROPIC_API_KEY=...
uvicorn app.main:app --reload
# open http://localhost:8000
```

Tests: `python -m unittest discover -s tests -v`

Docker: `docker compose up --build`

## Deploy

Full Arabic launch guide: **[docs/DEPLOY_AR.md](docs/DEPLOY_AR.md)**. What works out of the box vs. what needs external accounts: **[docs/STATUS_AR.md](docs/STATUS_AR.md)**.

## Security notes

- Passwords: scrypt. Sessions: signed JWT in an httpOnly, SameSite=Lax cookie; password change revokes other sessions. All writes require the `x-sahm` header (CSRF).
- Connected-account tokens are Fernet-encrypted at rest (`SAHM_ENCRYPTION_KEY`).
- Every query is scoped by `user_id`; isolation is covered by tests.
- Published client sites are served with `Content-Security-Policy: sandbox` (opaque origin) and, ideally, on a separate domain (`SAHM_SITES_BASE_URL`).
- Outbound HTTP from automations is blocked from private/internal addresses (SSRF guard).
- Webhooks: WhatsApp verified with `X-Hub-Signature-256`, Telegram with a per-bot secret token, Moyasar with the shared secret **and** a re-fetch of the invoice from Moyasar before activating a plan. Inbound events are de-duplicated.
- Login throttling, per-user chat rate limits, plan usage limits.
