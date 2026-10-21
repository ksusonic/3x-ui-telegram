import html
import io
import logging

import qrcode
from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from qrcode.exceptions import DataOverflowError

from .config import Config
from .panel import Panel, PanelError, fingerprint, telegram_id
from .store import ActionExpired, Store

log = logging.getLogger(__name__)
PAGE_SIZE = 8
INSTRUCTION = (
    "Откройте «Мои подключения», выберите нужное и скопируйте ссылку "
    "в VPN-приложение через импорт из буфера обмена. Можно также отсканировать QR-код.\n\n"
    "Если показана ссылка подписки, импортируйте её в приложении как подписку. "
    "Не передавайте ссылки и QR-коды другим людям: они дают доступ к вашему VPN."
)


def esc(value) -> str:
    return html.escape(str(value))


def markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=data) for title, data in row]
            for row in rows
        ]
    )


def menu():
    return markup(
        [
            [("Мои подключения", "menu:connections")],
            [("Статус", "menu:status"), ("Инструкция", "menu:instruction")],
        ]
    )


def used_bytes(value: int) -> str:
    amount = max(0, value)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if amount < 1024 or unit == "ТиБ":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def status_text(client: dict) -> str:
    traffic = client.get("traffic") or {}
    used = int(traffic.get("up", 0)) + int(traffic.get("down", 0))
    enabled = bool(client.get("enable")) and traffic.get("enable", True)
    return (
        f"<b>{esc(client['email'])}</b>\n"
        f"Состояние: {'включено' if enabled else 'отключено'}\n"
        f"Использовано: {used_bytes(used)}"
    )


class Guard(BaseMiddleware):
    def __init__(self, config: Config, panel: Panel, store: Store):
        self.config, self.panel, self.store = config, panel, store

    async def __call__(self, handler, event: Message | CallbackQuery, data):
        message = event if isinstance(event, Message) else event.message
        if not isinstance(message, Message) or event.from_user is None:
            return
        user_id = event.from_user.id
        authorized = user_id in self.config.admin_ids
        private = message.chat.type == "private"
        if not private and (
            message.chat.type not in {"group", "supergroup"}
            or message.chat.id not in self.config.admin_chat_ids
            or not authorized
            or (isinstance(event, Message) and event.sender_chat is not None)
        ):
            return
        # A group interaction does not prove a private chat exists.
        # Recording a private contact never grants access to VPN clients.
        if private:
            self.store.register(user_id, message.chat.id)
        try:
            is_admin = user_id in self.config.admin_ids
            clients = [] if is_admin else await self.panel.owned(user_id)
            if not is_admin and not clients:
                return
            authorized = True
            # Acknowledge callbacks before slower API operations to avoid query expiry.
            if isinstance(event, CallbackQuery):
                await event.answer()
            if not private:
                words = (event.text or "").split() if isinstance(event, Message) else []
                command = words[0].split("@", 1)[0] if words else ""
                personal = (
                    event.data and event.data.startswith(("key:", "menu:"))
                    if isinstance(event, CallbackQuery)
                    else command in {"/connections", "/status", "/instruction"}
                )
                if personal:
                    await message.answer(
                        "Личные подключения и статус доступны в личном чате с ботом"
                    )
                    return
            data.update(clients=clients, is_admin=is_admin)
            return await handler(event, data)
        except (PanelError, ActionExpired, ValueError) as exc:
            if not authorized:
                return
            # ValueError messages are not echoed: a parser may include sensitive input.
            text = (
                str(exc)
                if isinstance(exc, (PanelError, ActionExpired))
                else "Проверьте формат команды"
            )
            await message.answer(esc(text))
        except TelegramAPIError:
            log.warning("Telegram request failed for handler %s", type(event).__name__)
        except Exception as exc:
            # Never dump updates/tracebacks containing keys or tokens to logs.
            log.error("Handler failed: %s", type(exc).__name__)
            if authorized:
                await message.answer("Не удалось выполнить запрос. Сообщите администратору")


def create_router(config: Config, panel: Panel, store: Store) -> Router:
    router = Router()
    guard = Guard(config, panel, store)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    async def connections(message: Message, user_id: int):
        clients = await panel.owned(user_id)
        if not clients:
            await message.answer("К вашему Telegram ID пока не привязаны подключения")
            return
        rows = []
        for client in clients:
            token = store.action(user_id, "key", {"email": client["email"]}, ttl=3600)
            rows.append([(client["email"][:60], f"key:{token}")])
        # Telegram accepts at most 100 buttons per message.
        for offset in range(0, len(rows), 80):
            await message.answer(
                "Ваши подключения:", reply_markup=markup(rows[offset : offset + 80])
            )

    async def status(message: Message, user_id: int):
        clients = await panel.owned(user_id)
        if not clients:
            await message.answer("К вашему Telegram ID пока не привязаны подключения")
        for client in clients:
            await message.answer(status_text(client))

    @router.message(F.chat.type == "private", CommandStart())
    @router.message(F.chat.type == "private", Command("help"))
    async def start(message: Message, is_admin: bool):
        text = "Личный кабинет VPN. Выберите действие."
        if is_admin:
            text += (
                "\n\nКоманды администратора:\n"
                "/users [поиск] — клиенты и привязки\n"
                "/link ID [поиск] — привязать клиента\n"
                "/unlink ID [поиск] — снять привязку\n"
                "/notify ID текст — личное уведомление\n"
                "/broadcast текст — уведомление всем\n"
                "/jobs — последние рассылки\n"
                "/id — мой Telegram ID"
            )
        await message.answer(text, reply_markup=menu())

    @router.message(Command("id"))
    async def own_id(message: Message):
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(Command("connections"))
    async def connection_command(message: Message):
        await connections(message, message.from_user.id)

    @router.message(Command("status"))
    async def status_command(message: Message):
        await status(message, message.from_user.id)

    @router.message(Command("instruction"))
    async def instruction_command(message: Message):
        await message.answer(INSTRUCTION)

    @router.callback_query(F.data.startswith("menu:"))
    async def menu_callback(query: CallbackQuery):
        operation = query.data.split(":", 1)[1]
        if operation == "connections":
            await connections(query.message, query.from_user.id)
        elif operation == "status":
            await status(query.message, query.from_user.id)
        elif operation == "instruction":
            await query.message.answer(INSTRUCTION)

    @router.callback_query(F.data.startswith("key:"))
    async def key_callback(query: CallbackQuery):
        data = store.read_action(query.data.split(":")[1], query.from_user.id, "key")
        links, subscription = await panel.links(query.from_user.id, data["email"])
        if not links:
            await query.message.answer("Для этого клиента нет доступных ссылок подключения")
        for index, link in enumerate(links, start=1):
            if len(esc(link)) < 3500:
                await query.message.answer(
                    f"<b>{esc(data['email'])}</b> · {index}\n<code>{esc(link)}</code>",
                    protect_content=True,
                )
            else:
                await query.message.answer_document(
                    BufferedInputFile(link.encode(), filename=f"connection-{index}.txt"),
                    protect_content=True,
                )
            try:
                image = qrcode.make(link)
                output = io.BytesIO()
                image.save(output, format="PNG")
                await query.message.answer_photo(
                    BufferedInputFile(output.getvalue(), filename="vpn.png"),
                    caption=f"QR-код · {index}",
                    protect_content=True,
                )
            except DataOverflowError:
                await query.message.answer(
                    "Ссылка слишком длинная для QR-кода; импортируйте её из файла"
                )
        if subscription:
            await query.message.answer(
                f"Подписка:\n<code>{esc(subscription)}</code>",
                protect_content=True,
            )

    async def picker(message: Message, actor: int, mode: str, target: int, query: str, page: int):
        clients = sorted(
            [
                c
                for c in await panel.clients()
                if query.casefold() in c["email"].casefold()
                and (mode != "unlink" or telegram_id(c) == target)
            ],
            key=lambda c: c["email"].casefold(),
        )
        if not clients:
            await message.answer("Клиенты не найдены")
            return
        page = max(0, min(page, (len(clients) - 1) // PAGE_SIZE))
        rows = []
        for client in clients[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
            token = store.action(
                actor,
                "pick",
                {
                    "email": client["email"],
                    "target": target,
                    "mode": mode,
                },
            )
            label = f"{client['email']} · TG {telegram_id(client) or '—'}"
            rows.append([(label[:64], f"pick:{token}")])
        pages = []
        for next_page, title in ((page - 1, "←"), (page + 1, "→")):
            if 0 <= next_page <= (len(clients) - 1) // PAGE_SIZE:
                token = store.action(
                    actor,
                    "page",
                    {
                        "mode": mode,
                        "target": target,
                        "query": query,
                        "page": next_page,
                    },
                )
                pages.append((title, f"page:{token}"))
        if pages:
            rows.append(pages)
        await message.answer(
            f"Выберите клиента для {'привязки' if mode == 'link' else 'отвязки'} "
            f"Telegram ID <code>{target}</code>. Страница {page + 1}.",
            reply_markup=markup(rows),
        )

    async def require_admin(message: Message, is_admin: bool) -> bool:
        if not is_admin:
            await message.answer("Команда доступна только администраторам")
        return is_admin

    @router.message(Command("link", "unlink"))
    async def link_command(message: Message, is_admin: bool):
        if not await require_admin(message, is_admin):
            return
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
            await message.answer("Формат: /link ID [поиск] или /unlink ID [поиск]")
            return
        mode = parts[0].split("@")[0][1:]
        await picker(
            message,
            message.from_user.id,
            mode,
            int(parts[1]),
            parts[2] if len(parts) > 2 else "",
            0,
        )

    @router.callback_query(F.data.startswith("page:"))
    async def page_callback(query: CallbackQuery, is_admin: bool):
        if not await require_admin(query.message, is_admin):
            return
        data = store.consume(query.data.split(":")[1], query.from_user.id, "page")
        await picker(query.message, query.from_user.id, **data)

    @router.callback_query(F.data.startswith("pick:"))
    async def pick_callback(query: CallbackQuery, is_admin: bool):
        if not await require_admin(query.message, is_admin):
            return
        data = store.consume(query.data.split(":")[1], query.from_user.id, "pick")
        client = await panel.client(data["email"])
        if data["mode"] == "unlink" and telegram_id(client) != data["target"]:
            raise PanelError("Привязка изменилась. Повторите команду")
        target = data["target"] if data["mode"] == "link" else 0
        token = store.action(
            query.from_user.id,
            "bind",
            {
                "email": client["email"],
                "target": target,
                "expected": fingerprint(client),
                "previous": telegram_id(client),
            },
        )
        await query.message.answer(
            f"Клиент: <b>{esc(client['email'])}</b>\n"
            f"Текущий Telegram ID: {telegram_id(client) or 'нет'}\n"
            f"Новый Telegram ID: {target or 'нет (снять привязку)'}\nПодтвердите изменение.",
            reply_markup=markup(
                [[("Подтвердить", f"bind:{token}"), ("Отмена", f"cancel:bind:{token}")]]
            ),
        )

    @router.callback_query(F.data.startswith("bind:"))
    async def bind_callback(query: CallbackQuery, is_admin: bool):
        if not await require_admin(query.message, is_admin):
            return
        data = store.consume(query.data.split(":")[1], query.from_user.id, "bind")
        store.audit(
            query.from_user.id,
            "bind_requested",
            email=data["email"],
            previous=data["previous"],
            target=data["target"],
        )
        try:
            await panel.bind(data["email"], data["target"], data["expected"])
        except PanelError:
            store.audit(query.from_user.id, "bind_needs_review", email=data["email"])
            raise
        store.audit(
            query.from_user.id, "bind_completed", email=data["email"], target=data["target"]
        )
        await query.message.answer("Привязка обновлена")

    @router.message(Command("users"))
    async def users_command(message: Message, is_admin: bool):
        if not await require_admin(message, is_admin):
            return
        parts = message.text.split(maxsplit=1)
        query = parts[1].casefold() if len(parts) > 1 else ""
        clients = [c for c in await panel.clients() if query in c["email"].casefold()]
        lines = []
        for client in clients:
            user_id = telegram_id(client)
            contact = store.contact(user_id) if user_id else None
            if not user_id:
                status = "Telegram не привязан"
            elif contact is None:
                status = "чат с ботом ещё не зарегистрирован"
            elif not contact["reachable"]:
                status = "Telegram запретил отправку сообщений"
            else:
                status = "чат зарегистрирован"
            lines.append(f"{esc(client['email'])}: TG {user_id or '—'} · {status}")
        for offset in range(0, len(lines), 12):
            await message.answer("\n".join(lines[offset : offset + 12]))
        if not lines:
            await message.answer("Клиенты не найдены")

    @router.message(Command("notify", "broadcast"))
    async def notify_command(message: Message, is_admin: bool):
        if not await require_admin(message, is_admin):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Формат: /notify ID текст или /broadcast текст")
            return
        target = None
        body = parts[1]
        if parts[0].split("@")[0] == "/notify":
            recipient, body = parts[1].split(maxsplit=1)
            target = int(recipient)
        # Reserve room for the preview wrapper and respect Telegram's UTF-16 limit.
        if not body.strip() or len(body.encode("utf-16-le")) // 2 > 3000:
            await message.answer("Текст должен содержать от 1 до 3000 символов Telegram")
            return
        allowed = {telegram_id(c) for c in await panel.clients()} - {0}
        recipients = store.recipients(allowed if target is None else allowed & {target})
        if not recipients:
            await message.answer(
                "Нет доступных получателей. Пользователи должны быть привязаны и запустить /start"
            )
            return
        token = store.action(message.from_user.id, "send", {"body": body, "recipients": recipients})
        await message.answer(
            f"Получателей: {len(recipients)}\n\n{esc(body)}\n\nОтправить уведомление?",
            reply_markup=markup(
                [[("Отправить", f"send:{token}"), ("Отмена", f"cancel:send:{token}")]]
            ),
        )

    @router.callback_query(F.data.startswith("send:"))
    async def send_callback(query: CallbackQuery, is_admin: bool):
        if not await require_admin(query.message, is_admin):
            return
        token = query.data.split(":")[1]
        data = store.read_action(token, query.from_user.id, "send")
        allowed = {telegram_id(c) for c in await panel.clients()} - {0}
        recipients = store.recipients(allowed & set(data["recipients"]))
        job = store.enqueue(token, query.from_user.id, recipients)
        await query.message.answer(
            f"Рассылка №{job} поставлена в очередь. Получателей: {len(recipients)}"
        )

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_callback(query: CallbackQuery, is_admin: bool):
        if not await require_admin(query.message, is_admin):
            return
        _, kind, token = query.data.split(":")
        if kind not in {"bind", "send"}:
            return
        store.consume(token, query.from_user.id, kind)
        await query.message.answer("Действие отменено")

    @router.message(Command("jobs"))
    async def jobs_command(message: Message, is_admin: bool):
        if not await require_admin(message, is_admin):
            return
        jobs = store.recent_jobs()
        for job in jobs:
            counts = ", ".join(f"{key}: {value}" for key, value in store.counts(job["id"]).items())
            await message.answer(
                f"№{job['id']} · {'завершена' if job['finished'] else 'в очереди'} · {counts}"
            )
        if not jobs:
            await message.answer("Рассылок пока нет")

    @router.callback_query()
    async def unknown_callback(query: CallbackQuery):
        text = (
            "Кнопка устарела. Откройте /start"
            if query.message.chat.type == "private"
            else "Кнопка устарела. Повторите команду"
        )
        await query.message.answer(text)

    return router
