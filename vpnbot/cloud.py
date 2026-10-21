"""Private, queue-triggered Vercel subscriber. Each call performs bounded work."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions, Update
from vercel.queue import QueueError, RetryAfter, send, subscribe

from .cloud_config import CloudConfig
from .handlers import create_router
from .panel import Panel
from .postgres import PostgresStore
from .web import TOPIC
from .worker import deliver_one, report_finished

log = logging.getLogger(__name__)
for name in ("aiogram", "httpx", "httpcore", "httpx2", "httpcore2"):
    logging.getLogger(name).setLevel(logging.CRITICAL)


async def process(payload: dict, config: CloudConfig, store, panel, bot, publish):
    """Called only while holding the cross-deployment database session lock."""
    store.recover()
    update_id = None
    if payload.get("type") == "update":
        update = Update.model_validate(payload["update"])
        update_id = update.update_id
        if not store.update_done(update_id):
            dispatcher = Dispatcher()
            dispatcher.include_router(create_router(config.bot, panel, store))
            await dispatcher.feed_update(bot, update)
    elif payload.get("type") == "drain":
        if await deliver_one(bot, panel, store):
            store.cooldown(1.1)
    else:
        raise ValueError("Unknown queue work type")
    await report_finished(bot, store, config.bot)
    delay = store.continuation_delay()
    if delay is not None:
        # Must finish publishing continuation before acknowledging this delivery.
        await publish(TOPIC, {"type": "drain"}, delay=delay)
    if update_id is not None:
        store.mark_update(update_id)


@subscribe(topic=TOPIC, consumer_group="vpn-bot", max_concurrency=1, retry_after=5)
async def consume(payload: dict[str, object]):
    config = CloudConfig.from_env()
    store = None
    panel = None
    try:
        store = PostgresStore(config.database_url)
        if not store.try_lock():
            raise RetryAfter(2)
        store.initialize()
        panel = Panel(config.bot)
        async with Bot(
            config.bot.token,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview=LinkPreviewOptions(is_disabled=True),
            ),
        ) as bot:
            await process(payload, config, store, panel, bot, send)
    except RetryAfter:
        raise
    except QueueError:
        raise RetryAfter(10) from None
    except Exception as exc:
        # Exception messages can include DSNs or Telegram tokens. Only log the type.
        log.error("Queue work failed: %s", type(exc).__name__)
        raise RetryAfter(15) from None
    finally:
        if panel is not None:
            await panel.close()
        if store is not None:
            store.close()
