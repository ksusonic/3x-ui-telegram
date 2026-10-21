import copy
from datetime import UTC, datetime

import pytest
from aiogram.methods import EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import Chat, Message
from test_handlers import harness as harness

from vpnbot.cabinet import client_ref, context
from vpnbot.postgres import PostgresStore
from vpnbot.store import ActionExpired

LINK = (
    "vless://11111111-2222-4333-8444-555555555555@vpn.example:443"
    "?encryption=none&security=tls&sni=front.example&type=tcp"
)


def screen(calls):
    return next(
        c
        for c in reversed(calls)
        if isinstance(c, (SendMessage, EditMessageText))
        and c.reply_markup
        and hasattr(c.reply_markup, "inline_keyboard")
    )


def click(panel, label):
    return next(
        b.callback_data
        for row in panel.reply_markup.inline_keyboard
        for b in row
        if b.text == label
    )


def action(store, client, view, **extra):
    return "cab:" + store.action(200, "cabinet", {**context(client), "view": view, **extra})


def saved(store, client, app="happ", platform="ios", name=""):
    return store.save_device(200, client["email"], client_ref(client), platform, app, name)


async def test_start_opens_single_client_card_and_multiple_clients_picker(harness):
    send, panel, store = harness
    card = screen(await send(200, "/start"))
    assert "alice" in card.text and "4.0 КиБ" in card.text
    for label in ("Мои устройства", "Добавить устройство", "Ссылка", "QR-код", "Обновить"):
        assert click(card, label)
    panel.rows.append({**panel.rows[0], "id": 8, "email": "second"})
    picker = screen(await send(200, "/start"))
    assert "Выберите подключение" in picker.text
    assert click(picker, "alice") and click(picker, "second")


async def test_catalogue_pages_and_default_name_creation_are_idempotent(harness):
    send, panel, store = harness
    card = screen(await send(200, "/start"))
    platforms = screen(await send(200, callback=click(card, "Добавить устройство")))
    apps = screen(await send(200, callback=click(platforms, "iPhone / iPad")))
    assert "Страница 1 из 2" in apps.text
    first_labels = [b.text for row in apps.reply_markup.inline_keyboard[:8] for b in row]
    assert len(first_labels) == 8
    next_page = screen(await send(200, callback=click(apps, "→")))
    assert "Страница 2 из 2" in next_page.text
    assert isinstance(next_page, EditMessageText)
    naming = screen(await send(200, callback=click(apps, "Happ")))
    callback = click(naming, "Использовать название по умолчанию")
    guide = screen(await send(200, callback=callback))
    assert "Happ" in guide.text and "использования" in guide.text
    rows = store.devices(200, "alice", client_ref(panel.rows[0]))
    assert len(rows) == 1 and rows[0]["name"] == "iPhone / iPad — Happ"
    assert "uuid" not in rows[0].names
    await send(200, callback=callback)
    assert len(store.devices(200, "alice", client_ref(panel.rows[0]))) == 1
    saved(store, panel.rows[0])
    assert store.devices(200, "alice", client_ref(panel.rows[0]))[-1]["name"].endswith(" 2")


async def test_link_and_qr_are_separate_explicit_requests(harness):
    send, panel, store = harness
    panel.links_result = [LINK]
    card = screen(await send(200, "/start"))
    calls = await send(200, callback=click(card, "Ссылка"))
    assert any(
        isinstance(c, SendMessage) and LINK.replace("&", "&amp;") in c.text and c.protect_content
        for c in calls
    )
    assert not any(isinstance(c, SendPhoto) for c in calls)
    calls = await send(200, callback=click(card, "QR-код"))
    assert any(isinstance(c, SendPhoto) and c.protect_content for c in calls)
    assert not any(isinstance(c, SendMessage) and "vless://" in c.text for c in calls)


async def test_device_can_be_renamed_switched_and_deleted_without_panel_write(harness):
    send, panel, store = harness
    client = panel.rows[0]
    device = saved(store, client)
    guide = screen(await send(200, callback=action(store, client, "device", device=device["id"])))
    apps = screen(await send(200, callback=click(guide, "Сменить приложение")))
    updated = screen(await send(200, callback=click(apps, "Hiddify")))
    assert store.device(200, device["id"])["app_id"] == "hiddify"
    assert "Hiddify" in updated.text
    confirmation = screen(await send(200, callback=click(updated, "Удалить")))
    assert "VPN-ключ сохранится" in confirmation.text
    await send(200, callback=click(confirmation, "Удалить"))
    assert not store.devices(200, "alice", client_ref(client))
    assert not panel.bind_calls and client["tgId"] == 200


async def test_name_only_saved_on_reply_to_specific_prompt(harness):
    send, panel, store = harness
    client = panel.rows[0]
    calls = await send(
        200, callback=action(store, client, "select_app", platform="ios", app="happ")
    )
    prompt = next(
        c for c in calls if isinstance(c, SendMessage) and c.text.startswith("Как назвать")
    )
    pending = store.pending_name(200)
    assert pending is not None
    assert not await send(200, "ordinary group chatter")
    assert not store.devices(200, "alice", client_ref(client))
    reply = Message(
        message_id=pending["prompt_id"],
        date=datetime.now(UTC),
        chat=Chat(id=200, type="private"),
        text=prompt.text,
    )
    await send(200, "Мой iPhone <личный>", reply_to=reply)
    devices = store.devices(200, "alice", client_ref(client))
    assert len(devices) == 1 and devices[0]["name"] == "Мой iPhone <личный>"
    assert not store.pending_name(200)
    await send(200, "duplicate delivery", reply_to=reply)
    assert len(store.devices(200, "alice", client_ref(client))) == 1
    rename = action(store, client, "rename", device=devices[0]["id"])
    await send(200, callback=rename)
    pending = store.pending_name(200)
    reply = reply.model_copy(update={"message_id": pending["prompt_id"]})
    await send(200, "Новый телефон", reply_to=reply)
    assert store.device(200, devices[0]["id"])["name"] == "Новый телефон"


@pytest.mark.parametrize(
    "family,app,platform",
    [
        ("singbox", "singbox", "android"),
        ("mihomo", "clash-verge", "windows"),
        ("xray", "renoarx", "windows"),
        ("stash", "stash", "ios"),
    ],
)
async def test_requested_config_sent_as_protected_file(harness, family, app, platform):
    send, panel, store = harness
    client = panel.rows[0]
    panel.links_result = [LINK]
    device = saved(store, client, app, platform)
    calls = await send(200, callback=action(store, client, "file", device=device["id"]))
    documents = [c for c in calls if isinstance(c, SendDocument)]
    assert len(documents) == 1 and documents[0].protect_content
    assert family in documents[0].document.filename


async def test_multiple_links_require_selection_and_detect_reordering(harness):
    send, panel, store = harness
    client = panel.rows[0]
    panel.links_result = [LINK, LINK.replace("vpn.example", "second.example")]
    choices = screen(await send(200, callback=action(store, client, "send_link")))
    choice = click(choices, "Вариант 1")
    panel.links_result.reverse()
    calls = await send(200, callback=choice)
    assert not any(isinstance(c, SendMessage) and "vless://" in c.text for c in calls)
    assert any(isinstance(c, SendMessage) and "изменился" in c.text for c in calls)


@pytest.mark.parametrize("change", ["unlink", "replace", "disable"])
async def test_existing_device_callbacks_fail_closed_after_panel_change(harness, change):
    send, panel, store = harness
    client = panel.rows[0]
    panel.links_result = [LINK]
    device = saved(store, client)
    callback = action(store, client, "app_link", device=device["id"])
    if change == "unlink":
        client["tgId"] = 300
    elif change == "replace":
        client["uuid"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    else:
        client["enable"] = False
    calls = await send(200, callback=callback)
    assert not panel.link_calls
    assert not any(isinstance(c, (SendPhoto, SendDocument)) for c in calls)
    assert not any(isinstance(c, SendMessage) and "vless://" in c.text for c in calls)


async def test_client_ownership_rechecked_after_key_fetch(harness, monkeypatch):
    send, panel, store = harness
    client = panel.rows[0]
    panel.links_result = [LINK]
    callback = action(store, client, "send_link")
    original = panel.client

    async def revoked(email):
        row = copy.deepcopy(await original(email))
        row["tgId"] = 300
        return row

    monkeypatch.setattr(panel, "client", revoked)
    calls = await send(200, callback=callback)
    assert not any(isinstance(c, SendMessage) and "vless://" in c.text for c in calls)


async def test_foreign_actions_and_group_never_expose_devices(harness, config):
    send, panel, store = harness
    client = panel.rows[0]
    panel.rows.append({**client, "email": "bob", "tgId": 300, "id": 8})
    device = saved(store, client, name="Private device")
    callback = action(store, client, "device", device=device["id"])
    replies = await send(300, callback=callback)
    assert not any(
        isinstance(c, (SendMessage, EditMessageText)) and "Private device" in c.text
        for c in replies
    )
    object.__setattr__(config, "admin_chat_ids", frozenset({-100}))
    assert not [
        c
        for c in await send(100, callback=callback, chat_type="group")
        if isinstance(c, (SendMessage, EditMessageText))
    ]
    with pytest.raises(ActionExpired):
        store.device(300, device["id"])


async def test_incompatible_app_has_explanation_and_no_file(harness):
    send, panel, store = harness
    client = panel.rows[0]
    panel.links_result = [LINK.replace("type=tcp", "type=xhttp")]
    device = saved(store, client, "clash-verge", "windows")
    calls = await send(200, callback=action(store, client, "file", device=device["id"]))
    assert not any(isinstance(c, SendDocument) for c in calls)
    assert "Xray" in screen(calls).text
    assert click(screen(calls), "Выбрать другое приложение")


def test_migration_is_repeatable_and_preserves_existing_profiles(store, client):
    device = saved(store, client)
    store.initialize()
    assert store.device(200, device["id"])["app_id"] == "happ"
    secured = store.raw.execute(
        "SELECT relname FROM pg_class WHERE relnamespace='vpn_bot'::regnamespace AND relrowsecurity"
    ).fetchall()
    assert {"devices", "device_inputs", "schema_migrations"} <= {r[0] for r in secured}
    assert store.raw.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_devices_survive_new_connection(store, client):
    device = saved(store, client)
    dsn = store.raw.info.dsn
    store.close()
    second = PostgresStore(dsn, require_tls=False)
    try:
        assert second.try_lock()
        second.initialize()
        assert second.device(200, device["id"])["app_id"] == "happ"
    finally:
        second.close()


def test_upgrade_from_existing_schema_preserves_contacts_and_jobs(store):
    store.register(987654321, 987654321)
    store.raw.execute("DROP TABLE device_inputs,devices,schema_migrations")
    store.initialize()
    assert store.reachable(987654321)["chat_id"] == 987654321
    assert store.raw.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


async def test_expired_actions_do_not_write_device_or_send_secret(harness):
    send, panel, store = harness
    client = panel.rows[0]
    callback = action(store, client, "save_default", platform="ios", app="happ")
    store.db.execute("UPDATE actions SET expires=0")
    calls = await send(200, callback=callback)
    assert not store.devices(200, "alice", client_ref(client))
    assert not panel.link_calls
    assert not any(isinstance(c, (SendPhoto, SendDocument)) for c in calls)


async def test_invalid_and_expired_name_replies_do_not_change_devices(harness):
    send, panel, store = harness
    client = panel.rows[0]
    await send(200, callback=action(store, client, "select_app", platform="ios", app="happ"))
    pending = store.pending_name(200)
    reply = Message(
        message_id=pending["prompt_id"],
        date=datetime.now(UTC),
        chat=Chat(id=200, type="private"),
        text="Name prompt",
    )
    await send(200, "too\nlong", reply_to=reply)
    assert not store.devices(200, "alice", client_ref(client))
    store.db.execute("UPDATE device_inputs SET expires=0")
    assert not await send(200, "Late reply", reply_to=reply)
    assert not store.devices(200, "alice", client_ref(client))


async def test_disabled_connection_can_manage_profiles_but_not_fetch_keys(harness):
    send, panel, store = harness
    client = panel.rows[0]
    client["enable"] = False
    card = screen(await send(200, "/start"))
    assert "отключено" in card.text
    labels = [b.text for row in card.reply_markup.inline_keyboard for b in row]
    assert "Ссылка" not in labels and "QR-код" not in labels
    assert "Мои устройства" in labels


async def test_compatibility_alternatives_filter_catalogue(harness):
    send, panel, store = harness
    client = panel.rows[0]
    panel.links_result = [LINK.replace("type=tcp", "type=xhttp")]
    device = saved(store, client, "hiddify", "ios")
    error = screen(await send(200, callback=action(store, client, "app_link", device=device["id"])))
    apps = screen(await send(200, callback=click(error, "Выбрать другое приложение")))
    labels = [b.text for row in apps.reply_markup.inline_keyboard for b in row]
    assert "Happ" in labels
    assert "Hiddify" not in labels and "Loon" not in labels


async def test_deleted_message_falls_back_without_losing_profiles(harness, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    send, panel, store = harness
    card = screen(await send(200, "/start"))

    # Force the edit path to fail as Telegram does for a deleted anchor message.
    async def edit_missing(self, *args, **kwargs):
        raise TelegramBadRequest(
            method=EditMessageText(text="unused", chat_id=200, message_id=1),
            message="Bad Request: message to edit not found",
        )

    monkeypatch.setattr(Message, "edit_text", edit_missing)
    replacement = screen(await send(200, callback=click(card, "Мои устройства")))
    assert isinstance(replacement, SendMessage)
    assert "ещё не добавлены" in replacement.text
