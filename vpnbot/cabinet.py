"""Private Telegram cabinet. All callback contexts are actor-bound database actions."""

import hashlib
import html
import io
import json

import qrcode
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from qrcode.exceptions import DataOverflowError

from .catalog import PLATFORMS, RECIPES, compatibility, entry, instruction, platform_apps, snapshot
from .formats import Unsupported, export
from .panel import PanelError, telegram_id
from .store import ActionExpired

PAGE = 8


def client_ref(client):
    # UUID may itself be a VPN credential: persist only a one-way identity fingerprint.
    value = [
        client.get("id"),
        client.get("uuid"),
        client.get("createdAt", client.get("created_at")),
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def context(client):
    return {"email": client["email"], "ref": client_ref(client)}


def enabled(client):
    return bool(client.get("enable")) and (client.get("traffic") or {}).get("enable", True)


def status(client):
    traffic = client.get("traffic") or {}
    amount = max(0, int(traffic.get("up", 0)) + int(traffic.get("down", 0)))
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if amount < 1024 or unit == "ТиБ":
            break
        amount /= 1024
    state = "включено" if enabled(client) else "отключено"
    return f"Состояние: {state}\nИспользовано: {amount:.1f} {unit}"


class Cabinet:
    def __init__(self, panel, store):
        self.panel, self.store = panel, store
        # Fail early if a deployment omitted the packaged catalogue asset.
        snapshot()

    def button(self, actor, title, view, data=None, **extra):
        payload = {**(data or {}), **extra, "view": view}
        token = self.store.action(actor, "cabinet", payload, ttl=3600)
        return InlineKeyboardButton(text=title[:64], callback_data=f"cab:{token}")

    def keyboard(self, rows):
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def render(self, message, text, rows, *, edit):
        keyboard = self.keyboard(rows)
        if edit:
            try:
                await message.edit_text(text, reply_markup=keyboard)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message.lower():
                    return
                if not any(
                    s in exc.message.lower()
                    for s in (
                        "message to edit not found",
                        "message can't be edited",
                        "message_id_invalid",
                    )
                ):
                    raise
        await message.answer(text, reply_markup=keyboard)

    async def client(self, actor, data):
        rows = await self.panel.owned(actor)
        client = next(
            (
                c
                for c in rows
                if c["email"] == data.get("email") and client_ref(c) == data.get("ref")
            ),
            None,
        )
        if client is None:
            raise PanelError("Подключение больше не доступно. Откройте /start в личном чате")
        return client

    async def links(self, actor, data):
        client = await self.client(actor, data)
        if not enabled(client):
            raise PanelError("Подключение отключено. Обратитесь к администратору")
        links, _ = await self.panel.links(actor, client["email"])
        actual = await self.panel.client(client["email"])
        if telegram_id(actual) != actor or client_ref(actual) != data["ref"] or not enabled(actual):
            raise PanelError("Подключение изменилось. Повторите запрос")
        if not links:
            raise PanelError("Панель не вернула ссылки для этого подключения")
        return links

    async def home(self, message, actor, *, edit=False, page=0):
        self.store.clear_name(actor)
        clients = sorted(await self.panel.owned(actor), key=lambda c: c["email"].casefold())
        if len(clients) == 1:
            await self.card(message, actor, context(clients[0]), edit=edit)
            return
        page = max(0, min(int(page), max(0, (len(clients) - 1) // PAGE)))
        rows = [
            [self.button(actor, c["email"], "card", context(c))]
            for c in clients[page * PAGE : (page + 1) * PAGE]
        ]
        nav = []
        for p, label in ((page - 1, "←"), (page + 1, "→")):
            if 0 <= p <= (len(clients) - 1) // PAGE:
                nav.append(self.button(actor, label, "home", page=p))
        if nav:
            rows.append(nav)
        text = (
            "Выберите подключение:"
            if clients
            else "К вашему Telegram ID пока не привязаны подключения"
        )
        await self.render(message, text, rows, edit=edit)

    async def card(self, message, actor, data, *, edit=True):
        self.store.clear_name(actor)
        client = await self.client(actor, data)
        ctx = context(client)
        rows = [
            [self.button(actor, "Мои устройства", "devices", ctx)],
            [self.button(actor, "Добавить устройство", "platforms", ctx)],
        ]
        if enabled(client):
            rows.append(
                [
                    self.button(actor, "Ссылка", "send_link", ctx),
                    self.button(actor, "QR-код", "send_qr", ctx),
                ]
            )
        rows.extend(
            [
                [self.button(actor, "Обновить", "card", ctx)],
                [self.button(actor, "Все подключения", "home")],
            ]
        )
        await self.render(
            message, f"<b>{html.escape(client['email'])}</b>\n{status(client)}", rows, edit=edit
        )

    async def devices(self, message, actor, data):
        self.store.clear_name(actor)
        await self.client(actor, data)
        devices = self.store.devices(actor, data["email"], data["ref"])
        page = max(0, min(int(data.get("page", 0)), max(0, (len(devices) - 1) // PAGE)))
        rows = [
            [self.button(actor, d["name"], "device", data, device=d["id"])]
            for d in devices[page * PAGE : (page + 1) * PAGE]
        ]
        nav = [
            self.button(actor, label, "devices", data, page=p)
            for p, label in ((page - 1, "←"), (page + 1, "→"))
            if 0 <= p <= (len(devices) - 1) // PAGE
        ]
        if nav:
            rows.append(nav)
        rows.extend(
            [
                [self.button(actor, "Добавить устройство", "platforms", data)],
                [self.button(actor, "К подключению", "card", data)],
            ]
        )
        await self.render(
            message,
            "Ваши устройства:" if devices else "Устройства ещё не добавлены.",
            rows,
            edit=True,
        )

    async def platforms(self, message, actor, data):
        self.store.clear_name(actor)
        await self.client(actor, data)
        rows = [
            [self.button(actor, name, "apps", data, platform=p, page=0)]
            for p, name in PLATFORMS.items()
        ]
        rows.append([self.button(actor, "К подключению", "card", data)])
        await self.render(message, "На каком устройстве настроить VPN?", rows, edit=True)

    async def apps(self, message, actor, data):
        self.store.clear_name(actor)
        await self.client(actor, data)
        apps = platform_apps(data["platform"])
        if data.get("compatible_only"):
            links = await self.links(actor, data)
            apps = [
                app for app in apps if any(not compatibility(app["app_id"], link) for link in links)
            ]
        if not apps:
            await self.render(
                message,
                "Для этого профиля не найдено совместимых приложений "
                "на выбранной платформе. Обратитесь к администратору.",
                [[self.button(actor, "К подключению", "card", data)]],
                edit=True,
            )
            return
        page = max(0, min(int(data.get("page", 0)), (len(apps) - 1) // PAGE))
        rows = [
            [self.button(actor, a["name"], "select_app", data, app=a["app_id"])]
            for a in apps[page * PAGE : (page + 1) * PAGE]
        ]
        nav = [
            self.button(actor, label, "apps", data, page=p)
            for p, label in ((page - 1, "←"), (page + 1, "→"))
            if 0 <= p <= (len(apps) - 1) // PAGE
        ]
        if nav:
            rows.append(nav)
        rows.append(
            [self.button(actor, "Назад", "device" if data.get("device") else "platforms", data)]
        )
        rows.append([self.button(actor, "К подключению", "card", data)])
        await self.render(
            message,
            f"{PLATFORMS[data['platform']]}: выберите приложение.\n"
            f"Страница {page + 1} из {(len(apps) + PAGE - 1) // PAGE}",
            rows,
            edit=True,
        )

    def owned_device(self, actor, data):
        row = self.store.device(actor, data["device"])
        if row["email"] != data["email"] or row["client_ref"] != data["ref"]:
            raise ActionExpired("Устройство относится к другому подключению")
        return row

    async def guide(self, message, actor, data):
        await self.client(actor, data)
        device = self.owned_device(actor, data)
        app = entry(device["app_id"], device["platform"])
        ctx = {**data, "app": device["app_id"], "platform": device["platform"]}
        text = f"<b>{html.escape(device['name'])}</b>\n\n" + instruction(
            device["app_id"], device["platform"]
        )
        rows = [[InlineKeyboardButton(text="Установить", url=app["download_url"])]]
        family = RECIPES[device["app_id"]][0]
        if family == "uri":
            rows.append(
                [
                    self.button(actor, "Ссылка", "app_link", ctx),
                    self.button(actor, "QR-код", "app_qr", ctx),
                ]
            )
        elif family != "unsupported":
            rows.append([self.button(actor, "Файл конфигурации", "file", ctx)])
        else:
            text += "\n\nДля этого приложения нет совместимого способа настройки VLESS."
        rows.extend(
            [
                [self.button(actor, "Сменить приложение", "apps", ctx, page=0)],
                [
                    self.button(actor, "Переименовать", "rename", ctx),
                    self.button(actor, "Удалить", "delete_confirm", ctx),
                ],
                [self.button(actor, "Мои устройства", "devices", ctx)],
                [self.button(actor, "К подключению", "card", ctx)],
            ]
        )
        await self.render(message, text, rows, edit=True)

    async def name_screen(self, message, actor, data):
        await self.client(actor, data)
        app = entry(data["app"], data["platform"])
        rows = [
            [self.button(actor, "Использовать название по умолчанию", "save_default", data)],
            [self.button(actor, "Отмена", "card", data)],
        ]
        await self.render(
            message,
            f"Название устройства для {html.escape(app['name'])}.\n"
            "Ответьте на запрос ниже или используйте название по умолчанию.",
            rows,
            edit=True,
        )
        prompt = await message.answer(
            "Как назвать устройство? Ответьте на это сообщение.",
            reply_markup=ForceReply(selective=True),
        )
        self.store.name_prompt(actor, message.chat.id, prompt.message_id, message.message_id, data)

    def is_name_reply(self, message: Message):
        if message.chat.type != "private" or not message.from_user or not message.reply_to_message:
            return False
        pending = self.store.pending_name(message.from_user.id)
        return bool(
            pending
            and pending["chat_id"] == message.chat.id
            and pending["prompt_id"] == message.reply_to_message.message_id
        )

    def save(self, actor, data, name):
        entry(data["app"], data["platform"])
        if data.get("device"):
            row = self.owned_device(actor, data)
            self.store.edit_device(actor, row["id"], name=name or row["name"], app_id=data["app"])
        else:
            row = self.store.save_device(
                actor, data["email"], data["ref"], data["platform"], data["app"], name
            )
            data = {**data, "device": row["id"]}
        self.store.clear_name(actor)
        return data

    async def name_reply(self, message: Message):
        pending = self.store.pending_name(message.from_user.id)
        if not pending:
            return
        name = (message.text or "").strip()
        if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
            await message.answer(
                "Название: от 1 до 64 символов, без переносов строк. "
                "Ответьте на исходный запрос названия."
            )
            return
        data = json.loads(pending["payload"])
        await self.client(message.from_user.id, data)
        # Transactional with the device write: retries cannot create duplicate profiles.
        with self.store.db:
            data = self.save(message.from_user.id, data, name)
        anchor = Message(message_id=pending["anchor_id"], date=message.date, chat=message.chat)
        await self.guide(anchor.as_(message.bot), message.from_user.id, data)

    async def send_secret(self, message, actor, data, operation):
        links = await self.links(actor, data)
        app = None
        if operation in {"app_link", "app_qr", "file"}:
            device = self.owned_device(actor, data)
            app = device["app_id"]
            entry(app, device["platform"])
        # Select a link explicitly when a panel client has multiple inbounds.
        if len(links) > 1 and "link_index" not in data:
            rows = [
                [
                    self.button(
                        actor,
                        f"Вариант {i + 1}",
                        operation,
                        data,
                        link_index=i,
                        link_ref=hashlib.sha256(links[i].encode()).hexdigest(),
                    )
                ]
                for i in range(len(links))
            ]
            rows.append([self.button(actor, "К подключению", "card", data)])
            await self.render(
                message, "У подключения несколько вариантов. Выберите один:", rows, edit=True
            )
            return
        index = int(data.get("link_index", 0))
        if not 0 <= index < len(links):
            raise ActionExpired("Список вариантов изменился. Повторите запрос")
        link = links[index]
        if data.get("link_ref") and hashlib.sha256(link.encode()).hexdigest() != data["link_ref"]:
            raise ActionExpired("Выбранный вариант изменился. Повторите запрос")
        if app and (reason := compatibility(app, link)):
            rows = [
                [
                    self.button(
                        actor,
                        "Выбрать другое приложение",
                        "apps",
                        data,
                        page=0,
                        compatible_only=True,
                    )
                ],
                [self.button(actor, "К подключению", "card", data)],
            ]
            await self.render(
                message,
                "Этот профиль нельзя настроить выбранным способом:\n" + html.escape(reason),
                rows,
                edit=True,
            )
            return
        if operation == "file":
            filename, content = export(link, RECIPES[app][0])
            await message.answer_document(
                BufferedInputFile(content, filename=filename), protect_content=True
            )
        elif operation in {"send_qr", "app_qr"}:
            try:
                qr = qrcode.make(link)
                output = io.BytesIO()
                qr.save(output, format="PNG")
            except DataOverflowError:
                await message.answer(
                    "Ссылка слишком длинная для QR-кода. Используйте кнопку «Ссылка»."
                )
                return
            await message.answer_photo(
                BufferedInputFile(output.getvalue(), filename="vpn.png"),
                caption="QR-код подключения",
                protect_content=True,
            )
        elif len(html.escape(link)) <= 3500:
            await message.answer(f"<code>{html.escape(link)}</code>", protect_content=True)
        else:
            await message.answer_document(
                BufferedInputFile(link.encode(), filename="vpn-link.txt"), protect_content=True
            )

    async def callback(self, query: CallbackQuery):
        message = query.message
        if not isinstance(message, Message) or message.chat.type != "private":
            return
        actor = query.from_user.id
        token = query.data.split(":", 1)[1]
        data = self.store.read_action(token, actor, "cabinet")
        view = data["view"]
        if view == "home":
            await self.home(message, actor, edit=True, page=data.get("page", 0))
        elif view == "card":
            await self.card(message, actor, data)
        elif view == "devices":
            await self.devices(message, actor, data)
        elif view == "platforms":
            await self.platforms(message, actor, data)
        elif view == "apps":
            if data.get("device"):
                row = self.owned_device(actor, data)
                data = {**data, "platform": row["platform"]}
            await self.apps(message, actor, data)
        elif view == "select_app":
            await self.client(actor, data)
            entry(data["app"], data["platform"])
            if data.get("device"):
                row = self.owned_device(actor, data)
                if row["platform"] != data["platform"]:
                    raise ValueError("Платформа устройства изменилась")
                with self.store.db:
                    self.store.consume(token, actor, "cabinet")
                    self.store.edit_device(actor, row["id"], app_id=data["app"])
                await self.guide(message, actor, data)
            else:
                await self.name_screen(message, actor, data)
        elif view == "save_default":
            await self.client(actor, data)
            with self.store.db:
                self.store.consume(token, actor, "cabinet")
                data = self.save(actor, data, "")
            await self.guide(message, actor, data)
        elif view == "device":
            self.store.clear_name(actor)
            await self.guide(message, actor, data)
        elif view == "rename":
            row = self.owned_device(actor, data)
            await self.name_screen(
                message, actor, {**data, "app": row["app_id"], "platform": row["platform"]}
            )
        elif view == "delete_confirm":
            await self.client(actor, data)
            row = self.owned_device(actor, data)
            await self.render(
                message,
                f"Удалить устройство «{html.escape(row['name'])}»?\n"
                "Это удалит сохранённый профиль устройства. VPN-ключ сохранится.",
                [
                    [
                        self.button(actor, "Удалить", "delete", data),
                        self.button(actor, "Отмена", "device", data),
                    ]
                ],
                edit=True,
            )
        elif view == "delete":
            await self.client(actor, data)
            self.owned_device(actor, data)
            with self.store.db:
                self.store.consume(token, actor, "cabinet")
                self.store.delete_device(actor, data["device"])
                self.store.clear_name(actor)
            await self.devices(message, actor, data)
        elif view in {"send_link", "send_qr", "app_link", "app_qr", "file"}:
            try:
                await self.send_secret(message, actor, data, view)
            except Unsupported as exc:
                await message.answer(html.escape(str(exc)))

    def register(self, router):
        router.callback_query.register(self.callback, F.data.startswith("cab:"))
        router.message.register(self.name_reply, F.text, F.reply_to_message, self.is_name_reply)
