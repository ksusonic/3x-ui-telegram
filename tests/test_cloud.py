from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from vercel.queue import DuplicateIdempotencyKeyError

from vpnbot.cloud import process
from vpnbot.cloud_config import CloudConfig
from vpnbot.postgres import PostgresStore
from vpnbot.web import MAX_BODY, app

SECRET = "x" * 48
UPDATE = {
    "update_id": 123,
    "message": {
        "message_id": 1,
        "date": 0,
        "chat": {"id": 200, "type": "private"},
        "from": {"id": 200, "is_bot": False, "first_name": "Alice"},
        "text": "/start",
    },
}


@pytest.fixture
async def web(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("VERCEL_ENV", "production")
    publish = AsyncMock()
    monkeypatch.setattr("vpnbot.web.send", publish)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://bot.example"
    ) as client:
        yield client, publish


async def test_webhook_secret_is_required(web):
    client, publish = web
    for secret in ("", "wrong-secret"):
        response = await client.post(
            "/api/telegram",
            json=UPDATE,
            headers={
                "X-Telegram-Bot-Api-Secret-Token": secret,
            },
        )
        assert response.status_code == 403
    publish.assert_not_called()


async def test_webhook_publishes_durable_update_before_ack(web):
    client, publish = web
    response = await client.post(
        "/api/telegram",
        json=UPDATE,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
        },
    )
    assert response.status_code == 200
    assert publish.call_args.kwargs["idempotency_key"] == "telegram-update-123"
    assert publish.call_args.args[1]["update"]["update_id"] == 123


async def test_duplicate_queue_publish_is_acknowledged(web):
    client, publish = web
    publish.side_effect = DuplicateIdempotencyKeyError("duplicate")
    response = await client.post(
        "/api/telegram",
        json=UPDATE,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
        },
    )
    assert response.status_code == 200


async def test_queue_failure_is_not_acknowledged_or_exposed(web):
    client, publish = web
    publish.side_effect = RuntimeError("secret credentials in upstream error")
    response = await client.post(
        "/api/telegram",
        json=UPDATE,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
        },
    )
    assert response.status_code == 503
    assert "credentials" not in response.text


async def test_invalid_and_oversized_body_never_reaches_queue(web):
    client, publish = web
    for body, expected in ((b"invalid-json", 400), (b"x" * (MAX_BODY + 1), 413)):
        response = await client.post(
            "/api/telegram",
            content=body,
            headers={
                "X-Telegram-Bot-Api-Secret-Token": SECRET,
            },
        )
        assert response.status_code == expected
    publish.assert_not_called()


async def test_preview_never_uses_production_bot(web, monkeypatch):
    client, publish = web
    monkeypatch.setenv("VERCEL_ENV", "preview")
    response = await client.post(
        "/api/telegram",
        json=UPDATE,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
        },
    )
    assert response.status_code == 503
    publish.assert_not_called()


async def test_health_has_no_sensitive_metadata(web):
    client, publish = web
    assert (await client.get("/health")).json() == {"ok": True}
    publish.assert_not_called()


async def test_readiness_requires_secret(web):
    client, publish = web
    response = await client.post("/api/ready")
    assert response.status_code == 403
    assert response.json() == {"ok": False}
    publish.assert_not_called()


async def test_update_replay_does_not_dispatch_twice(config, store, monkeypatch):
    dispatcher = SimpleNamespace(include_router=Mock(), feed_update=AsyncMock())
    monkeypatch.setattr("vpnbot.cloud.Dispatcher", lambda: dispatcher)
    cloud = CloudConfig(config, "unused", SECRET)
    panel, bot, publish = AsyncMock(), AsyncMock(), AsyncMock()
    payload = {"type": "update", "update": UPDATE}
    await process(payload, cloud, store, panel, bot, publish)
    await process(payload, cloud, store, panel, bot, publish)
    dispatcher.feed_update.assert_awaited_once()
    assert store.update_done(123)


async def test_drain_sends_one_recipient_and_schedules_report(config, store, client):
    store.register(200, 200)
    token = store.action(100, "send", {"body": "hello"})
    job = store.enqueue(token, 100, [200])
    panel, bot, publish = AsyncMock(), AsyncMock(), AsyncMock()
    panel.owned.return_value = [client]
    cloud = CloudConfig(config, "unused", SECRET)
    await process({"type": "drain"}, cloud, store, panel, bot, publish)
    assert store.counts(job) == {"sent": 1}
    bot.send_message.assert_awaited_once_with(200, "hello", parse_mode=None)
    assert publish.call_args.kwargs["delay"] >= 1
    with store.db:
        store.db.execute("UPDATE cooldown SET until_at=0")
    await process({"type": "drain"}, cloud, store, panel, bot, publish)
    assert store.continuation_delay() is None
    assert "завершена" in bot.send_message.call_args.args[1]


async def test_publish_failure_preserves_sent_delivery_for_retry(config, store, client):
    store.register(200, 200)
    token = store.action(100, "send", {"body": "hello"})
    job = store.enqueue(token, 100, [200])
    panel, bot, publish = AsyncMock(), AsyncMock(), AsyncMock()
    panel.owned.return_value = [client]
    cloud = CloudConfig(config, "unused", SECRET)
    publish.side_effect = RuntimeError("queue offline")
    with pytest.raises(RuntimeError):
        await process({"type": "drain"}, cloud, store, panel, bot, publish)
    assert store.counts(job) == {"sent": 1}
    publish.side_effect = None
    await process({"type": "drain"}, cloud, store, panel, bot, publish)
    bot.send_message.assert_awaited_once_with(200, "hello", parse_mode=None)


def test_processing_lease_prevents_overlapping_deployments(store):
    second = PostgresStore(store.raw.info.dsn, require_tls=False)
    try:
        assert not second.try_lock()
        store.close()
        assert second.try_lock()
    finally:
        second.close()


def test_expired_lease_can_be_reclaimed_and_old_owner_cannot_release_it(store):
    second = PostgresStore(store.raw.info.dsn, require_tls=False)
    try:
        assert not second.try_lock()
        store.raw.execute(
            "UPDATE vpn_bot.work_lock SET expires_at=clock_timestamp()-interval '1 second'"
        )
        assert second.try_lock()
        store.close()
        assert (
            second.raw.execute("SELECT owner FROM vpn_bot.work_lock").fetchone()[0] == second.owner
        )
    finally:
        second.close()


def test_postgres_atomic_rollback(store):
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("INSERT INTO chats VALUES (987654321,987654321,1)")
        raise RuntimeError("rollback")
    assert not store.reachable(987654321)
