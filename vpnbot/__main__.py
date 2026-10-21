import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
from urllib.parse import quote

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from .config import Config
from .handlers import create_router
from .panel import Panel, PanelError, telegram_id, update_payload
from .store import Store
from .worker import run_worker


async def main(panel_only: bool):
    os.umask(0o077)
    config = Config.from_env(panel_only=panel_only)
    panel = Panel(config)
    try:
        clients = await panel.clients()
        if panel_only:
            # Read-only: doesn't print names, keys, tgIds or panel credentials.
            if clients:
                full = await panel.client(clients[0]["email"])
                update_payload(full, telegram_id(full))
                links = await panel.request(
                    "GET",
                    f"panel/api/clients/links/{quote(full['email'], safe='')}",
                )
                if not isinstance(links, list):
                    raise PanelError("Неожиданный формат API ссылок подключения")
            print(
                f"API клиентов доступен. Клиентов: {len(clients)}. "
                f"С Telegram ID: {sum(telegram_id(c) > 0 for c in clients)}."
            )
            return
        config.database.parent.mkdir(parents=True, exist_ok=True)
        # A single local startup open, before polling or the queue worker starts.
        with open(str(config.database) + ".lockfile", "a") as lock:  # noqa: ASYNC230
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Бот с этой базой уже запущен") from None
            store = Store(config.database)
            store.recover()
            try:
                async with Bot(
                    config.token,
                    default=DefaultBotProperties(
                        parse_mode=ParseMode.HTML,
                        link_preview=LinkPreviewOptions(is_disabled=True),
                    ),
                ) as bot:
                    webhook = await bot.get_webhook_info()
                    if webhook.url:
                        raise ValueError(
                            "У бота настроен webhook. Удалите его перед запуском polling"
                        )
                    dispatcher = Dispatcher()
                    dispatcher.include_router(create_router(config, panel, store))
                    task = asyncio.create_task(run_worker(bot, panel, store, config))
                    try:
                        await dispatcher.start_polling(
                            bot,
                            allowed_updates=["message", "callback_query"],
                            handle_as_tasks=False,
                        )
                    finally:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            finally:
                store.close()
    finally:
        await panel.close()


def cli():
    parser = argparse.ArgumentParser(description="Private 3x-ui Telegram bot")
    parser.add_argument(
        "--check-panel", action="store_true", help="Read-only API compatibility check"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # These libraries may include request URLs or incoming update contents in logs.
    for name in ("httpx", "httpcore", "aiogram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(main(args.check_panel))
    except (ValueError, PanelError) as exc:
        # Config parse errors can contain input values; do not echo arbitrary ValueErrors.
        print(
            str(exc)
            if isinstance(exc, PanelError)
            else "Проверьте конфигурацию, доступность БД, "
            "отсутствие webhook и второго экземпляра бота."
        )
        raise SystemExit(1) from None
    except TelegramAPIError:
        print(
            "Не удалось подключиться к Telegram. "
            "Проверьте токен и отсутствие другого polling-процесса."
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
