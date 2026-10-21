from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from vpnbot.postgres import PostgresStore
from vpnbot.store import ActionExpired
from vpnbot.worker import deliver_one, report_finished


def queue(store, ids=(200,)):
    token = store.action(100, "send", {"body": "Hello <plain text>", "recipients": list(ids)})
    return store.enqueue(token, 100, list(ids))


def test_actions_are_bound_to_actor_type_and_expiration(store):
    token = store.action(100, "bind", {"email": "alice"})
    for actor, kind in [(200, "bind"), (100, "send")]:
        with pytest.raises(ActionExpired):
            store.consume(token, actor, kind)
    assert store.consume(token, 100, "bind") == {"email": "alice"}
    with pytest.raises(ActionExpired):
        store.consume(token, 100, "bind")
    expired = store.action(100, "bind", {}, ttl=-1)
    with pytest.raises(ActionExpired):
        store.consume(expired, 100, "bind")


def test_duplicate_confirmation_cannot_create_second_job(store):
    token = store.action(100, "send", {"body": "hi", "recipients": [200]})
    job = store.enqueue(token, 100, [200, 200])
    with pytest.raises(ActionExpired):
        store.enqueue(token, 100, [200])
    assert store.counts(job) == {"pending": 1}
    assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_restart_marks_inflight_unknown_and_keeps_pending(store):
    dsn = store.raw.info.dsn
    job = queue(store, (200, 201))
    store.delivery(job, 200, "sending")
    store.close()
    reopened = PostgresStore(dsn, require_tls=False)
    assert reopened.try_lock()
    reopened.initialize()
    reopened.recover()
    assert reopened.counts(job) == {"unknown": 1, "pending": 1}
    assert reopened.next_delivery()["user_id"] == 201
    reopened.close()


async def test_delivery_rechecks_access_after_enqueue(store):
    job = queue(store)
    store.register(200, 200)
    panel, bot = AsyncMock(), AsyncMock()
    panel.owned.return_value = []
    await deliver_one(bot, panel, store)
    bot.send_message.assert_not_called()
    assert store.counts(job) == {"skipped": 1}


async def test_plain_text_delivery(store, client):
    store.register(200, 200)
    job = queue(store)
    panel, bot = AsyncMock(), AsyncMock()
    panel.owned.return_value = [client]
    assert await deliver_one(bot, panel, store)
    bot.send_message.assert_awaited_once_with(200, "Hello <plain text>", parse_mode=None)
    assert store.counts(job) == {"sent": 1}


@pytest.mark.parametrize(
    "error,state",
    [
        (
            TelegramNetworkError(method=SendMessage(chat_id=200, text="hi"), message="timeout"),
            "unknown",
        ),
        (
            TelegramForbiddenError(method=SendMessage(chat_id=200, text="hi"), message="blocked"),
            "blocked",
        ),
    ],
)
async def test_delivery_failure_policy(store, client, error, state):
    store.register(200, 200)
    job = queue(store)
    panel, bot = AsyncMock(), AsyncMock()
    panel.owned.return_value = [client]
    bot.send_message.side_effect = error
    await deliver_one(bot, panel, store)
    assert store.counts(job) == {state: 1}
    if state == "blocked":
        assert not store.reachable(200)
        store.register(200, 200)
        assert store.reachable(200)


async def test_flood_control_requeues_without_marking_sent(store, client):
    store.register(200, 200)
    job = queue(store)
    panel, bot = AsyncMock(), AsyncMock()
    panel.owned.return_value = [client]
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=200, text="hi"),
        message="slow",
        retry_after=2,
    )
    await deliver_one(bot, panel, store)
    assert store.counts(job) == {"pending": 1}
    assert store.next_delivery() is None
    assert store.cooling_down()


async def test_panel_outage_defers_delivery(store):
    from vpnbot.panel import PanelError

    store.register(200, 200)
    job = queue(store)
    panel, bot = AsyncMock(), AsyncMock()
    panel.owned.side_effect = PanelError("Unavailable")
    assert not await deliver_one(bot, panel, store)
    assert store.counts(job) == {"pending": 1}
    bot.send_message.assert_not_called()


async def test_summary_sent_once(store, config):
    job = queue(store)
    store.delivery(job, 200, "unknown")
    bot = AsyncMock()
    await report_finished(bot, store, config)
    await report_finished(bot, store, config)
    bot.send_message.assert_awaited_once()
    assert "Результат неизвестен: 1" in bot.send_message.call_args.args[1]
