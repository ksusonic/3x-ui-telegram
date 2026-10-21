import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from .config import Config
from .panel import Panel, PanelError, telegram_id
from .store import Store

log = logging.getLogger(__name__)


async def deliver_one(bot: Bot, panel: Panel, store: Store) -> bool:
    item = store.next_delivery()
    if item is None:
        return False
    job, user_id = item["job_id"], item["user_id"]
    # Recheck membership immediately before each delivery, including after restart.
    try:
        clients = await panel.owned(user_id)
    except PanelError:
        store.delivery(job, user_id, "pending", delay=30)
        return False
    contact = store.reachable(user_id)
    if not contact or not any(telegram_id(c) == user_id for c in clients):
        store.delivery(job, user_id, "skipped")
        return True
    store.delivery(job, user_id, "sending")
    try:
        await bot.send_message(contact["chat_id"], item["body"], parse_mode=None)
    except TelegramRetryAfter as exc:
        store.delivery(job, user_id, "pending", delay=exc.retry_after + 1)
        # Telegram's flood limit applies to the sender, not just this recipient.
        store.cooldown(exc.retry_after + 1)
    except TelegramForbiddenError:
        store.blocked(user_id)
        store.delivery(job, user_id, "blocked")
    except TelegramBadRequest:
        store.delivery(job, user_id, "failed")
    except TelegramAPIError:
        store.delivery(job, user_id, "unknown")
        log.warning("Delivery outcome unknown: job=%s user=%s", job, user_id)
    else:
        store.delivery(job, user_id, "sent")
    return True


async def report_finished(bot: Bot, store: Store, config: Config):
    if store.cooling_down():
        return
    for job in store.finish():
        if job["actor"] not in config.admin_ids:
            store.reported(job["id"])
            continue
        counts = store.counts(job["id"])
        text = (
            f"Рассылка №{job['id']} завершена.\n"
            f"Доставлено: {counts.get('sent', 0)}\n"
            f"Недоступны: {counts.get('blocked', 0)}\n"
            f"Пропущено после изменения доступа: {counts.get('skipped', 0)}\n"
            f"Ошибок: {counts.get('failed', 0)}\n"
            f"Результат неизвестен: {counts.get('unknown', 0)}"
        )
        try:
            await bot.send_message(job["actor"], text, parse_mode=None)
        except TelegramRetryAfter as exc:
            store.cooldown(exc.retry_after + 1)
            return
        except TelegramAPIError:
            # Reports are available via /jobs; don't repeatedly send an uncertain summary.
            log.warning("Job report failed: job=%s", job["id"])
        store.reported(job["id"])
