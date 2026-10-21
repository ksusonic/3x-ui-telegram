import os
import re
from dataclasses import dataclass, field

from .config import Config


@dataclass(frozen=True)
class CloudConfig:
    bot: Config
    database_url: str = field(repr=False)
    webhook_secret: str = field(repr=False)

    @classmethod
    def from_env(cls):
        database = os.environ.get("DATABASE_URL", "")
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if not database.startswith(("postgres://", "postgresql://")):
            raise ValueError("Set DATABASE_URL to the Supabase session-pooler PostgreSQL URL")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError("Set TELEGRAM_WEBHOOK_SECRET to a random 32–256 character secret")
        if os.environ.get("VERCEL_ENV") == "preview":
            raise ValueError("Bot processing is disabled in preview deployments")
        return cls(Config.from_env(), database, secret)
