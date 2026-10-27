"""Runtime configuration, read once from the environment (.env supported)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load .env in development
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    env: str = field(default_factory=lambda: _env("SAHM_ENV", "development"))
    base_url: str = field(default_factory=lambda: _env("SAHM_BASE_URL", "http://localhost:8000").rstrip("/"))
    # Generated sites are served from here. Use a separate (sub)domain in production.
    sites_base_url: str = field(default_factory=lambda: _env("SAHM_SITES_BASE_URL", "").rstrip("/"))
    data_dir: Path = field(default_factory=lambda: Path(_env("SAHM_DATA_DIR", str(ROOT / "data"))))
    secret_key: str = field(default_factory=lambda: _env("SAHM_SECRET_KEY"))
    encryption_key: str = field(default_factory=lambda: _env("SAHM_ENCRYPTION_KEY"))
    run_worker_in_process: bool = field(default_factory=lambda: _bool("SAHM_RUN_WORKER", True))
    timezone: str = field(default_factory=lambda: _env("SAHM_TIMEZONE", "Asia/Riyadh"))
    admin_emails: tuple = field(default_factory=lambda: tuple(e.strip().lower() for e in _env("SAHM_ADMIN_EMAILS").split(",") if e.strip()))

    # Model provider (Anthropic Messages API)
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    anthropic_fast_model: str = field(default_factory=lambda: _env("ANTHROPIC_FAST_MODEL", "claude-haiku-5-5"))
    anthropic_base_url: str = field(default_factory=lambda: _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"))

    # Email (any SMTP provider: Resend, Amazon SES, Zoho, Google Workspace…)
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587") or 587))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", "SAHM | شهم <no-reply@example.com>"))

    # Meta (Facebook Pages + Instagram + WhatsApp Cloud API)
    meta_app_id: str = field(default_factory=lambda: _env("META_APP_ID"))
    meta_app_secret: str = field(default_factory=lambda: _env("META_APP_SECRET"))
    meta_graph_version: str = field(default_factory=lambda: _env("META_GRAPH_VERSION", "v26.0"))
    whatsapp_verify_token: str = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))

    # TikTok
    tiktok_client_key: str = field(default_factory=lambda: _env("TIKTOK_CLIENT_KEY"))
    tiktok_client_secret: str = field(default_factory=lambda: _env("TIKTOK_CLIENT_SECRET"))

    # Payments (Moyasar)
    moyasar_secret_key: str = field(default_factory=lambda: _env("MOYASAR_SECRET_KEY"))
    moyasar_webhook_secret: str = field(default_factory=lambda: _env("MOYASAR_WEBHOOK_SECRET"))

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sahm.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def public_sites_base(self) -> str:
        return self.sites_base_url or f"{self.base_url}/s"

    def validate(self) -> list[str]:
        """Return a list of fatal configuration problems."""
        problems = []
        if self.is_production:
            if len(self.secret_key) < 32:
                problems.append("SAHM_SECRET_KEY must be at least 32 characters in production")
            if not self.encryption_key:
                problems.append("SAHM_ENCRYPTION_KEY is required in production (see .env.example)")
            if not self.base_url.startswith("https://"):
                problems.append("SAHM_BASE_URL must be https:// in production")
        return problems


settings = Settings()


def reload_settings() -> Settings:
    """Re-read the environment (used by tests)."""
    global settings
    settings = Settings()
    return settings
