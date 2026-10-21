"""Explicitly register the production Telegram webhook after deployment checks."""

import argparse
import asyncio
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .cloud_config import CloudConfig
from .config import https_prefix


async def register(url: str):
    config = CloudConfig.from_env()
    https_prefix(url, "webhook URL")
    if urlsplit(url).path != "/api/telegram":
        raise ValueError("Webhook URL must end with /api/telegram")
    async with Bot(config.bot.token) as bot:
        await bot.set_webhook(
            url,
            secret_token=config.webhook_secret,
            allowed_updates=["message", "callback_query"],
            max_connections=1,
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        if info.url != url:
            raise ValueError("Webhook registration could not be verified")
        print("Webhook зарегистрирован. Ожидающих обновлений:", info.pending_update_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Production HTTPS URL ending in /api/telegram")
    args = parser.parse_args()
    try:
        asyncio.run(register(args.url))
    except (ValueError, TelegramAPIError):
        print("Не удалось зарегистрировать webhook. Проверьте URL, окружение и токен")
        raise SystemExit(1) from None
