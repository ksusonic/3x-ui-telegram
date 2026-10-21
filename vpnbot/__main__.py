"""Read-only API compatibility check; the bot runs exclusively on Vercel."""

import argparse
import asyncio
import logging
from urllib.parse import quote

from .config import Config
from .panel import Panel, PanelError, telegram_id, update_payload


async def check_panel():
    panel = Panel(Config.from_env(panel_only=True))
    try:
        clients = await panel.clients()
        if clients:
            client = await panel.client(clients[0]["email"])
            update_payload(client, telegram_id(client))
            links = await panel.request(
                "GET",
                f"panel/api/clients/links/{quote(client['email'], safe='')}",
            )
            if not isinstance(links, list):
                raise PanelError("Неожиданный формат API ссылок подключения")
        print(
            f"API клиентов доступен. Клиентов: {len(clients)}. "
            f"С Telegram ID: {sum(telegram_id(c) > 0 for c in clients)}."
        )
    finally:
        await panel.close()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-panel", action="store_true", required=True)
    parser.parse_args()
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(check_panel())
    except (ValueError, PanelError) as exc:
        print(str(exc) if isinstance(exc, PanelError) else "Проверьте конфигурацию API панели")
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
