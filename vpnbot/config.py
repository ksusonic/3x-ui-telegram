import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def https_prefix(value: str, name: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError(f"{name}: нужен HTTPS URL без логина, пароля и query-параметров")
    return value.rstrip("/") + "/"


@dataclass(frozen=True)
class Config:
    panel_url: str
    admin_ids: frozenset[int]
    token: str = field(repr=False)
    panel_token: str = field(default="", repr=False)
    subscription_url: str = ""
    ca_file: str = ""

    @classmethod
    def from_env(cls, *, panel_only: bool = False) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        ids = os.environ.get("ADMIN_IDS", "")
        admins = frozenset(int(x.strip()) for x in ids.split(",") if x.strip())
        if not panel_only and (not token or not admins or any(x <= 0 for x in admins)):
            raise ValueError("Задайте BOT_TOKEN и положительные числовые ADMIN_IDS")
        panel_token = os.environ.get("PANEL_API_TOKEN", "").strip()
        if not panel_token:
            raise ValueError("Задайте PANEL_API_TOKEN")
        sub = os.environ.get("SUBSCRIPTION_BASE_URL", "").strip()
        return cls(
            panel_url=https_prefix(os.environ.get("PANEL_BASE_URL", ""), "PANEL_BASE_URL"),
            admin_ids=admins,
            token=token,
            panel_token=panel_token,
            subscription_url=https_prefix(sub, "SUBSCRIPTION_BASE_URL") if sub else "",
            ca_file=os.environ.get("PANEL_CA_FILE", ""),
        )
