import asyncio
import copy
import hashlib
import json
import ssl
from urllib.parse import quote

import httpx

from .config import Config


class PanelError(Exception):
    """Safe for display: never includes response bodies, credentials or URLs."""


class StaleClient(PanelError):
    pass


def telegram_id(client: dict) -> int:
    try:
        return int(client.get("tgId") or 0)
    except (TypeError, ValueError):
        return 0


def fingerprint(client: dict) -> str:
    # Traffic and server timestamps change without administrative edits.
    value = {
        k: v
        for k, v in client.items()
        if k not in {"traffic", "usedTraffic", "updated_at", "updatedAt"}
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def update_payload(client: dict, target: int) -> dict:
    payload = copy.deepcopy(client)
    # GET returns ClientRecord.id (integer) + uuid. POST expects Client.id (UUID).
    # Never send the numeric record ID as a protocol credential.
    record_id = payload.pop("id", None)
    uuid = payload.pop("uuid", None)
    if uuid is not None:
        payload["id"] = uuid
    elif isinstance(record_id, str):
        payload["id"] = record_id
    for source, target_key in (("createdAt", "created_at"), ("updatedAt", "updated_at")):
        if source in payload:
            payload[target_key] = payload.pop(source)
    if isinstance(payload.get("allowedIPs"), str):
        value = payload["allowedIPs"]
        payload["allowedIPs"] = [ip.strip() for ip in value.split(",") if ip.strip()]
    if payload.get("tunnelAllowedIPs"):
        payload["allowedIPsByInbound"] = {
            key: [ip.strip() for ip in value.split(",") if ip.strip()]
            for key, value in payload["tunnelAllowedIPs"].items()
        }
    for key in (
        "traffic",
        "inboundIds",
        "externalConfigIds",
        "externalLinks",
        "usedTraffic",
        "tunnelAllowedIPs",
    ):
        payload.pop(key, None)
    payload["tgId"] = target
    return payload


class Panel:
    def __init__(self, config: Config, transport=None):
        self.config = config
        self.http = httpx.AsyncClient(
            base_url=config.panel_url,
            verify=ssl.create_default_context(cafile=config.ca_file or None),
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Authorization": f"Bearer {config.panel_token}"},
        )
        self._edit_lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("success") is not True:
                raise PanelError("Панель отклонила запрос; проверьте её состояние")
            return data.get("obj")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise PanelError("Панель недоступна или вернула неожиданный ответ") from None

    async def clients(self) -> list[dict]:
        result = await self.request("GET", "panel/api/clients/list")
        if not isinstance(result, list) or any(
            not isinstance(c, dict) or not isinstance(c.get("email"), str) for c in result
        ):
            raise PanelError("API клиентов несовместим: требуется актуальная версия 3x-ui")
        return result

    async def client(self, email: str) -> dict:
        result = await self.request("GET", f"panel/api/clients/get/{quote(email, safe='')}")
        if isinstance(result, dict) and isinstance(result.get("client"), dict):
            result = {**result["client"], **{k: v for k, v in result.items() if k != "client"}}
        if not isinstance(result, dict) or result.get("email") != email:
            raise PanelError("Клиент отсутствует или API вернул неожиданный ответ")
        return result

    async def owned(self, user_id: int) -> list[dict]:
        # Use one consistent full projection for authorization, status and shared-subId checks.
        return [c for c in await self.clients() if telegram_id(c) == user_id]

    async def bind(self, email: str, target: int, expected: str):
        async with self._edit_lock:
            client = await self.client(email)
            if fingerprint(client) != expected:
                raise StaleClient("Клиент изменился. Повторите команду и проверьте новую привязку")
            payload = update_payload(client, target)
            try:
                await self.request(
                    "POST",
                    f"panel/api/clients/update/{quote(email, safe='')}",
                    json=payload,
                )
                actual = await self.client(email)
            except PanelError:
                raise PanelError(
                    "Результат изменения не подтверждён; возможна частичная запись. "
                    "Проверьте клиента в 3x-ui перед повторной попыткой"
                ) from None
            before, after = update_payload(client, 0), update_payload(actual, 0)
            for obj in (before, after):
                obj.pop("updated_at", None)
            if telegram_id(actual) != target or before != after:
                raise PanelError("Результат обновления требует проверки администратором в 3x-ui")

    async def links(self, user_id: int, email: str) -> tuple[list[str], str]:
        clients = await self.clients()
        client = next((c for c in clients if c["email"] == email), None)
        if not client or telegram_id(client) != user_id:
            raise PanelError("Подключение больше не привязано к вашему Telegram ID")
        if not client.get("enable"):
            return [], ""
        result = await self.request("GET", f"panel/api/clients/links/{quote(email, safe='')}")
        if not isinstance(result, list) or any(not isinstance(x, str) for x in result):
            raise PanelError("Панель вернула неожиданный формат подключений")
        # A subscription can contain several clients. Withhold it if it exposes another owner.
        sub = client.get("subId")
        shared = sub and any(c.get("subId") == sub and telegram_id(c) != user_id for c in clients)
        subscription = (
            self.config.subscription_url + quote(sub, safe="")
            if sub and not shared and self.config.subscription_url
            else ""
        )
        # Recheck after the link request in case an admin unlinked the user meanwhile.
        if telegram_id(await self.client(email)) != user_id:
            raise PanelError("Привязка изменилась. Повторите запрос")
        return result, subscription
