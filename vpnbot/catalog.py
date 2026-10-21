"""Reviewed catalogue snapshot and explicit import recipes, without runtime scraping."""

import html
import json
from functools import lru_cache
from pathlib import Path

from .formats import Unsupported, export, parse_vless

PLATFORMS = {
    "ios": "iPhone / iPad",
    "android": "Android",
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
}
OFFICIAL_GUIDES = {
    "happ": "https://github.com/Happ-proxy/happ-desktop",
    "hiddify": "https://hiddify.com/app/How-to-use-Hiddify-app/",
    "singbox": "https://sing-box.sagernet.org/clients/",
    "stash": "https://stash.wiki/en/proxy-protocols/proxy-types",
    "loon": "https://nsloon.app/docs/Node/",
    "anywhere": "https://github.com/NodePassProject/Anywhere",
    "inhive": "https://app.inhive.ru/",
    "renoarx": "https://github.com/RonnyFX/RenoarX",
}
# App-specific UI paths. Version-dependent labels are deliberately described as alternatives.
RECIPES = {
    "happ": ("uri", "Нажмите «+» и выберите добавление из буфера обмена."),
    "v2rayng": ("uri", "Откройте «+» → «Импорт из буфера обмена» и выберите добавленный сервер."),
    "v2rayn": (
        "uri",
        "В меню «Серверы» выберите импорт из буфера обмена и сделайте сервер активным.",
    ),
    "v2raytun": ("uri", "Нажмите «+» и выберите импорт из буфера обмена."),
    "v2box": ("uri", "Откройте Configs, нажмите «+» и выберите импорт из буфера обмена."),
    "streisand": ("uri", "Нажмите «+» и выберите импорт из буфера обмена."),
    "shadowrocket": (
        "uri",
        "Скопируйте ссылку, откройте Shadowrocket и подтвердите импорт из буфера.",
    ),
    "simple-gui-client": ("uri", "В разделе профилей выберите импорт ссылки из буфера обмена."),
    "simplexray": ("uri", "В списке серверов выберите добавление из буфера обмена."),
    "onexray": ("uri", "Откройте список конфигураций и добавьте ссылку из буфера обмена."),
    "incy": ("uri", "Откройте добавление сервера и импортируйте скопированную ссылку VLESS."),
    "hiddify": ("uri", "Нажмите «+» → «Добавить из буфера обмена», затем выберите профиль."),
    "karing": ("uri", "Откройте добавление профиля и выберите импорт из буфера обмена."),
    "nekobox": ("uri", "Нажмите «+» → «Импорт из буфера обмена», затем выберите профиль."),
    "husi": ("uri", "В списке профилей выберите импорт из буфера обмена."),
    "throne": ("uri", "В меню профилей выберите импорт из буфера обмена и активируйте профиль."),
    "renoarx": (
        "xray",
        "Сохраните JSON-файл. В списке профилей импортируйте полный Xray JSON из файла.",
    ),
    "singbox": ("singbox", "Сохраните JSON-файл. Создайте локальный профиль и выберите этот файл."),
    "deskbox": (
        "singbox",
        "Сохраните JSON-файл и выберите его как конфигурацию sing-box в DeskBox.",
    ),
    "clash-meta-android": (
        "mihomo",
        "Сохраните YAML-файл. Откройте «Профили» → «Новый профиль» → «Файл».",
    ),
    "clash-mi": (
        "mihomo",
        "Сохраните YAML-файл и добавьте его как локальный профиль в разделе конфигураций.",
    ),
    "clash-verge": (
        "mihomo",
        "Сохраните YAML-файл. В разделе «Профили» импортируйте локальный файл.",
    ),
    "clashmac": ("mihomo", "Сохраните YAML-файл и импортируйте его как локальную конфигурацию."),
    "flclash": ("mihomo", "Сохраните YAML-файл. Откройте «Профили», нажмите «+» и выберите файл."),
    "flclashx": ("mihomo", "Сохраните YAML-файл. В разделе «Профили» добавьте профиль из файла."),
    "flowvy": (
        "mihomo",
        "Сохраните YAML-файл и добавьте локальный профиль в разделе конфигураций.",
    ),
    "koalaclash": ("mihomo", "Сохраните YAML-файл и импортируйте его в разделе «Профили»."),
    "prizrakbox": (
        "mihomo",
        "Сохраните YAML-файл. В разделе профилей выберите импорт локального файла.",
    ),
    "rabbit-hole": ("mihomo", "Сохраните YAML-файл и добавьте его как локальную конфигурацию."),
    "stash": (
        "stash",
        "Сохраните YAML-файл. В Stash откройте конфигурации и импортируйте локальный файл.",
    ),
    "loon": (
        "loon",
        "Сохраните файл .conf. В разделе конфигураций Loon импортируйте "
        "локальный файл и выберите его активным.",
    ),
    "anywhere": ("uri", "Откройте добавление прокси и вставьте ссылку VLESS в поле импорта."),
    "inhive": ("uri", "В приложении InHive откройте добавление профиля и вставьте ссылку VLESS."),
}


@lru_cache
def snapshot():
    return json.loads(Path(__file__).with_name("clients.json").read_text())


def get_app(app_id):
    entries = [e for e in snapshot()["entries"] if e["app_id"] == app_id]
    if not entries or app_id not in RECIPES:
        raise ValueError("Приложение отсутствует в каталоге")
    return entries[0]


def platform_apps(platform):
    if platform not in PLATFORMS:
        raise ValueError("Неизвестная платформа")
    return [e for e in snapshot()["entries"] if e["platform"] == platform]


def entry(app_id, platform):
    return next(e for e in platform_apps(platform) if e["app_id"] == app_id)


def compatibility(app_id, link):
    family, _ = RECIPES[app_id]
    if family == "unsupported":
        return RECIPES[app_id][1]
    try:
        v = parse_vless(link)
        app = get_app(app_id)
        if family == "uri":
            transport = v.options.get("type", "tcp")
            if app["core"] == "singbox" and v.options.get("support-x25519mlkem768") in {
                "true",
                "1",
            }:
                raise Unsupported(
                    "Этот вариант Reality требует ML-KEM. "
                    "Выберите актуальное приложение с ядром Xray"
                )
            if app["core"] == "singbox" and app_id != "inhive" and transport == "xhttp":
                raise Unsupported("Для XHTTP выберите приложение с ядром Xray")
            if app_id in {"anywhere", "shadowrocket"} and transport not in {
                "tcp",
                "raw",
                "ws",
                "grpc",
            }:
                raise Unsupported("Для этого транспорта используйте приложение с ядром Xray")
        else:
            export(link, family)
    except Unsupported as exc:
        return str(exc)
    return ""


def instruction(app_id, platform):
    app = entry(app_id, platform)
    family, recipe = RECIPES[app_id]
    escape = html.escape
    if family == "unsupported":
        steps = recipe + " Выберите другое приложение для настройки этого подключения."
    elif family == "uri":
        steps = "Нажмите «Ссылка» ниже и скопируйте её целиком. " + recipe
    else:
        steps = "Нажмите «Файл конфигурации» ниже. " + recipe
    if platform in {"ios", "android"}:
        connect = "Выберите профиль, нажмите подключение и разрешите создание VPN-соединения."
    elif platform == "macos":
        connect = "Выберите профиль и разрешите VPN/сетевое расширение в настройках macOS."
    else:
        connect = "Выберите профиль и включите системный прокси или TUN/VPN-режим приложения."
    if app_id in {"singbox", "deskbox", "renoarx"}:
        connect += " В профиле локальный SOCKS5 доступен только на 127.0.0.1:1080."
    problem = (
        "Если импорт не работает, обновите приложение и проверьте, что выбран нужный "
        "способ: ссылка сервера или локальный файл, а не URL подписки. "
        "Если нет соединения, проверьте состояние подключения в боте "
        "и временно отключите другой VPN."
    )
    source = OFFICIAL_GUIDES.get(app_id) or next(
        (u for u in app["sources"][1:] if not u.startswith("https://t.me/")), app["download_url"]
    )
    return (
        f"<b>{escape(app['name'])} · {escape(PLATFORMS[platform])}</b>\n\n"
        f"1. Установите приложение по кнопке «Установить».\n"
        f"2. {escape(steps)}\n3. {escape(connect)}\n\n{escape(problem)}\n\n"
        f'<a href="{escape(source, quote=True)}">Официальный источник</a>\n'
        "Выбор приложения сохраняется в боте; это не определение фактического использования."
    )
