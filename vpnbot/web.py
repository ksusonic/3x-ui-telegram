"""Fast webhook ingress. No state or work is left in the function after its response."""

import hmac
import logging
import os

from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from vercel.queue import DuplicateIdempotencyKeyError, QueueError

from .network import publish as send

TOPIC = "vpn-bot-work"
MAX_BODY = 256 * 1024
log = logging.getLogger(__name__)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    # Liveness only; never probes the panel or reveals secrets/configuration.
    return {"ok": True}


@app.post("/api/ready")
async def ready(request: Request):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if len(secret) < 32 or not hmac.compare_digest(secret.encode(), supplied.encode()):
        return JSONResponse({"ok": False}, status_code=403)
    if os.environ.get("VERCEL_ENV") == "preview":
        return JSONResponse({"ok": False}, status_code=503)
    from .cloud_config import CloudConfig
    from .panel import Panel
    from .postgres import PostgresStore

    store = None
    panel = None
    try:
        config = CloudConfig.from_env()
        store = PostgresStore(config.database_url)
        if not store.try_lock():
            return JSONResponse({"ok": False}, status_code=503)
        store.initialize()
        panel = Panel(config.bot)
        await panel.clients()
        return {"ok": True}
    except Exception as exc:
        log.warning("Readiness check failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False}, status_code=503)
    finally:
        if panel is not None:
            await panel.close()
        if store is not None:
            store.close()


@app.post("/api/telegram")
async def telegram(request: Request):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if len(secret) < 32 or os.environ.get("VERCEL_ENV") == "preview":
        return JSONResponse({"ok": False}, status_code=503)
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret.encode(), supplied.encode()):
        return JSONResponse({"ok": False}, status_code=403)
    # Bound body size before JSON parsing, including chunked requests.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            return JSONResponse({"ok": False}, status_code=413)
    try:
        update = Update.model_validate_json(bytes(body))
    except ValidationError:
        return JSONResponse({"ok": False}, status_code=400)
    if update.message is None and update.callback_query is None:
        return {"ok": True}
    try:
        await send(
            TOPIC,
            {"type": "update", "update": update.model_dump(mode="json", exclude_none=True)},
            idempotency_key=f"telegram-update-{update.update_id}",
        )
    except DuplicateIdempotencyKeyError:
        pass
    except QueueError:
        # Telegram retries a non-2xx response. Never acknowledge a lost queue publish.
        log.warning("Telegram update could not be queued")
        return JSONResponse({"ok": False}, status_code=503)
    except Exception as exc:
        log.warning("Queue publish failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}
