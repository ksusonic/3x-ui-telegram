import copy
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from vpnbot.handlers import create_router, status_text
from vpnbot.panel import PanelError, fingerprint


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return Message(
            message_id=len(self.calls),
            date=datetime.now(UTC),
            chat=Chat(id=int(method.chat_id), type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="VPN"),
            text=getattr(method, "text", None),
            reply_markup=getattr(method, "reply_markup", None),
        )

    async def stream_content(self, *args, **kwargs):
        yield b""


class FakePanel:
    def __init__(self, clients):
        self.rows = clients
        self.bind_calls = []
        self.link_calls = []
        self.unavailable = False

    async def clients(self):
        if self.unavailable:
            raise PanelError("Панель недоступна")
        return copy.deepcopy(self.rows)

    async def owned(self, user_id):
        return [row for row in await self.clients() if row["tgId"] == user_id]

    async def client(self, email):
        return next(row for row in await self.clients() if row["email"] == email)

    async def bind(self, email, target, expected):
        row = next(row for row in self.rows if row["email"] == email)
        if fingerprint(row) != expected:
            raise PanelError("Клиент изменился")
        self.bind_calls.append((email, target))
        row["tgId"] = target

    async def links(self, user_id, email):
        self.link_calls.append((user_id, email))
        row = await self.client(email)
        if row["tgId"] != user_id:
            raise PanelError("Чужое подключение")
        return ["vless://private-key@vpn.example:443"], ""


@pytest.fixture
async def harness(config, client, store):
    session = RecordingSession()
    panel = FakePanel([copy.deepcopy(client)])
    bot = Bot(
        config.token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(config, panel, store))
    seq = 0

    async def send(user_id, text=None, callback=None, chat_type="private"):
        nonlocal seq
        seq += 1
        user = User(id=user_id, is_bot=False, first_name="Person")
        chat = Chat(id=user_id if chat_type == "private" else -100, type=chat_type)
        if callback:
            message = Message(
                message_id=seq,
                date=datetime.now(UTC),
                chat=chat,
                from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
                text="Menu",
            )
            update = Update(
                update_id=seq,
                callback_query=CallbackQuery(
                    id=str(seq),
                    from_user=user,
                    chat_instance="instance",
                    message=message,
                    data=callback,
                ),
            )
        else:
            entities = []
            if text and text.startswith("/"):
                entities = [
                    MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))
                ]
            update = Update(
                update_id=seq,
                message=Message(
                    message_id=seq,
                    date=datetime.now(UTC),
                    chat=chat,
                    from_user=user,
                    text=text,
                    entities=entities,
                ),
            )
        start = len(session.calls)
        await dispatcher.feed_update(bot, update)
        return session.calls[start:]

    yield send, panel, store
    await bot.session.close()


def messages(calls):
    return [call for call in calls if isinstance(call, SendMessage)]


def button(call, prefix):
    return next(
        b.callback_data
        for row in call.reply_markup.inline_keyboard
        for b in row
        if b.callback_data.startswith(prefix + ":")
    )


async def test_closed_access_has_id_but_no_registration_request(harness):
    send, panel, store = harness
    replies = messages(await send(999, "/start"))
    assert "Доступ закрыт" in replies[0].text
    assert "999" in replies[0].text
    assert replies[0].reply_markup is None
    assert panel.rows[0]["tgId"] == 200
    # Starting is only a contact record, never an authorization grant.
    assert store.reachable(999)


async def test_group_commands_do_nothing_even_for_admin(harness):
    send, panel, store = harness
    assert not await send(100, "/link 300 alice", chat_type="group")
    assert not panel.bind_calls


async def test_nonadmin_cannot_bind_or_broadcast(harness):
    send, panel, store = harness
    for command in ("/link 300 alice", "/broadcast Hello", "/users"):
        replies = messages(await send(200, command))
        assert "только администраторам" in replies[0].text
    assert not panel.bind_calls
    assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


async def test_bind_requires_confirmation_and_rejects_replay(harness):
    send, panel, store = harness
    picker = messages(await send(100, "/link 300 ali"))[0]
    confirmation = messages(await send(100, callback=button(picker, "pick")))[0]
    assert "200" in confirmation.text and "300" in confirmation.text
    assert not panel.bind_calls
    await send(100, callback=button(confirmation, "bind"))
    assert panel.bind_calls == [("alice", 300)]
    replay = messages(await send(100, callback=button(confirmation, "bind")))
    assert "устарела" in replay[0].text
    assert len(panel.bind_calls) == 1


async def test_user_cannot_use_admin_confirmation(harness):
    send, panel, store = harness
    picker = messages(await send(100, "/link 300 alice"))[0]
    confirmation = messages(await send(100, callback=button(picker, "pick")))[0]
    await send(200, callback=button(confirmation, "bind"))
    assert not panel.bind_calls
    # An unauthorized click does not consume the admin's confirmation.
    await send(100, callback=button(confirmation, "bind"))
    assert panel.bind_calls == [("alice", 300)]


async def test_key_callback_is_bound_to_user_and_rechecks_revocation(harness):
    send, panel, store = harness
    panel.rows.append({**panel.rows[0], "email": "bob", "tgId": 300})
    response = messages(await send(200, "/connections"))[0]
    callback = button(response, "key")
    await send(300, callback=callback)
    assert not panel.link_calls
    panel.rows[0]["tgId"] = 0
    replies = await send(200, callback=callback)
    assert not panel.link_calls
    assert any(
        isinstance(call, AnswerCallbackQuery) and "Доступ закрыт" in call.text for call in replies
    )


async def test_authorized_key_includes_qr(harness):
    send, panel, store = harness
    response = messages(await send(200, "/connections"))[0]
    calls = await send(200, callback=button(response, "key"))
    from aiogram.methods import SendPhoto

    assert any(isinstance(c, SendPhoto) and c.protect_content for c in calls)
    assert any("vless://private-key" in c.text and c.protect_content for c in messages(calls))


async def test_broadcast_deduplicates_and_respects_confirmation(harness):
    send, panel, store = harness
    panel.rows.append({**panel.rows[0], "email": "alice-phone"})
    await send(200, "/start")
    response = messages(await send(100, "/broadcast Привет <друзья>"))[0]
    assert "Получателей: 1" in response.text
    assert "&lt;друзья&gt;" in response.text
    assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    await send(100, callback=button(response, "send"))
    assert store.next_delivery()["body"] == "Привет <друзья>"
    await send(100, callback=button(response, "send"))
    assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


async def test_panel_outage_fails_closed_for_existing_buttons(harness):
    send, panel, store = harness
    response = messages(await send(200, "/connections"))[0]
    panel.unavailable = True
    replies = messages(await send(200, callback=button(response, "key")))
    assert "недоступна" in replies[0].text
    assert not panel.link_calls


async def test_whitespace_message_is_safe(harness):
    send, panel, store = harness
    assert messages(await send(200, "   "))


def test_status_omits_all_quotas_and_expiration(client):
    result = status_text(client)
    assert "4.0 КиБ" in result
    assert "включено" in result
    assert str(client["totalGB"]) not in result
    assert str(client["expiryTime"]) not in result
    assert not any(word in result.lower() for word in ("квот", "лимит", "срок", "истека"))
