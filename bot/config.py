"""All settings come from environment variables (GitHub Actions secrets / Render env)."""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    # An unset GitHub secret arrives as "", so treat empty the same as missing.
    return (os.environ.get(name) or default).strip()


@dataclass
class Config:
    # --- LLM (Groq, OpenAI-compatible API) ---
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    # Check console.groq.com/docs/models for what is currently available on the free tier.
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "openai/gpt-oss-120b"))
    llm_fallback_model: str = field(default_factory=lambda: _env("LLM_FALLBACK_MODEL", "openai/gpt-oss-20b"))

    # --- Telegram ---
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    # Only this chat may talk to the bot / receive messages. Protects your Groq quota.
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_webhook_secret: str = field(default_factory=lambda: _env("TELEGRAM_WEBHOOK_SECRET"))

    # --- Memory (Supabase). If unset, falls back to a local JSON file (dev/testing only). ---
    supabase_url: str = field(default_factory=lambda: _env("SUPABASE_URL").rstrip("/"))
    supabase_key: str = field(default_factory=lambda: _env("SUPABASE_SERVICE_KEY"))
    local_store_path: str = field(default_factory=lambda: _env("LOCAL_STORE_PATH", "munim_local_store.json"))

    # --- Runtime safety ---
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "production").lower())
    allow_local_store: bool = field(default_factory=lambda: _env("ALLOW_LOCAL_STORE") == "1")
    scheduled_delivery: bool = field(default_factory=lambda: _env("MUNIM_SCHEDULED") == "1")

    # --- Behaviour ---
    bot_name: str = field(default_factory=lambda: _env("BOT_NAME", "Munim"))
    timezone: str = field(default_factory=lambda: _env("BOT_TZ", "Asia/Kolkata"))
    max_notes_in_context: int = field(default_factory=lambda: int(_env("MAX_NOTES_IN_CONTEXT", "28")))
    dry_run: bool = field(default_factory=lambda: _env("DRY_RUN") == "1")  # print instead of sending

    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    def validate_runtime(self, *, webhook: bool = False) -> None:
        if self.app_env == "production" and not self.has_supabase():
            raise RuntimeError("Production requires SUPABASE_URL and SUPABASE_SERVICE_KEY; local JSON storage is not persistent on hosted runners.")
        if not self.has_supabase() and not self.allow_local_store and self.app_env != "test":
            raise RuntimeError("Local JSON storage is disabled. Set ALLOW_LOCAL_STORE=1 for deliberate local development.")
        if webhook and not all((self.telegram_webhook_secret, self.telegram_token, self.telegram_chat_id,
                                self.groq_api_key)):
            raise RuntimeError("Webhook mode requires TELEGRAM_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN, "
                               "TELEGRAM_CHAT_ID and GROQ_API_KEY.")
