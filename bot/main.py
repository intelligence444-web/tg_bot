"""Функция bot: вебхук Telegram через API Gateway (спец. 3, 13, 17).

Переменные окружения: BOT_TOKEN, KIE_API_KEY, WEBHOOK_SECRET, ADMIN_CHAT_ID (необязательна),
YDB_ENDPOINT, YDB_DATABASE, VISION_PRIMARY, VISION_FALLBACK (необязательна); PRIVACY_URL читает
``bot.texts``, YDB_METADATA_CREDENTIALS — ``bot.ydb_storage``.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from bot.storage import Storage

log = logging.getLogger(__name__)

SECRET_HEADER = "x-telegram-bot-api-secret-token"
VISION_TIMEOUT_S = 45.0

_OK = {"statusCode": 200, "body": ""}


def _header(headers: dict[str, Any] | None, name: str) -> str | None:
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value
    return None


def _body(event: dict[str, Any]) -> Any:
    raw = event.get("body")
    if raw is None:
        raise ValueError("empty body")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw, validate=True)
    return json.loads(raw)


def _user_id(update: Update) -> int | None:
    try:
        return update.event.from_user.id
    except Exception:  # неизвестный тип апдейта или событие без from_user
        return None


async def process_webhook(
    event: dict, dp: Dispatcher, bot: Bot, storage: Storage, secret: str, now: datetime
) -> dict:
    """Проверка секрета, разбор тела, отсечение повторов и передача апдейта в aiogram."""
    got = _header(event.get("headers"), SECRET_HEADER)
    if not secret or got is None or not hmac.compare_digest(got.encode(), secret.encode()):
        log.warning("webhook: bad secret", extra={"user_id": None, "error_kind": "bad_secret"})
        return {"statusCode": 403, "body": ""}
    try:
        update = Update.model_validate(_body(event), context={"bot": bot})
    except Exception:  # неверный base64/JSON или не Update (pydantic ValidationError)
        log.warning("webhook: bad body", extra={"user_id": None, "error_kind": "bad_body"})
        return {"statusCode": 400, "body": ""}

    # Отмечаем до обработки: упавший апдейт не повторяется (спец. 13: «Повтор update_id → пропуск»).
    if not await storage.mark_processed(f"update:{update.update_id}", now):
        return _OK
    try:
        await dp.feed_update(bot, update)
    except Exception:
        log.exception(
            "update failed", extra={"user_id": _user_id(update), "error_kind": "handler"}
        )
    return _OK


# --- рантайм Cloud Functions: объекты создаются один раз на процесс ---

_runtime: dict[str, Any] = {}
_init_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _lock() -> asyncio.Lock:
    """Замок инициализации для текущего цикла событий (asyncio.Lock привязан к циклу)."""
    global _init_lock
    loop = asyncio.get_running_loop()
    if _init_lock is None or _init_lock[0] is not loop:
        _init_lock = (loop, asyncio.Lock())
    return _init_lock[1]


async def _close_runtime(rt: dict[str, Any]) -> None:
    """Закрыть старые клиенты, насколько получится: они могли остаться от другого цикла."""
    from bot import ydb_storage  # noqa: PLC0415

    closers = (
        ("bot", lambda: rt["bot"].session.close()),
        ("http", lambda: rt["http"].aclose()),
        ("pool", lambda: ydb_storage.close(rt["pool"])),
    )
    for name, close in closers:
        if name not in rt:
            continue
        try:
            await close()
        except Exception:
            log.warning("runtime: failed to close %s", name, exc_info=True, extra={"user_id": None, "error_kind": "close"})


async def _init_runtime() -> dict[str, Any]:
    import httpx  # noqa: PLC0415

    from bot import ydb_storage  # noqa: PLC0415
    from bot.app import Deps, build_dispatcher  # noqa: PLC0415
    from bot.vision import build_vision  # noqa: PLC0415

    bot = Bot(token=os.environ["BOT_TOKEN"])
    http = httpx.AsyncClient(timeout=VISION_TIMEOUT_S)
    pool = await ydb_storage.connect(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"])
    storage = ydb_storage.YdbStorage(pool)
    vision = build_vision(
        os.environ["VISION_PRIMARY"],
        os.environ.get("VISION_FALLBACK") or None,
        os.environ["KIE_API_KEY"],
        http,
    )

    async def fetch_photo(bot: Bot, file_id: str) -> bytes:
        buf = await bot.download(file_id)
        return buf.getvalue()

    admin = os.environ.get("ADMIN_CHAT_ID", "").strip()
    deps = Deps(
        storage=storage,
        vision=vision,
        fetch_photo=fetch_photo,
        clock=lambda: datetime.now(timezone.utc),
        admin_chat_id=int(admin) if admin else None,
        rng=random.Random(),
    )
    dp = build_dispatcher(deps, ydb_storage.YdbFsmStorage(pool))
    return {
        "loop": asyncio.get_running_loop(),
        "bot": bot,
        "dp": dp,
        "storage": storage,
        "pool": pool,
        "http": http,
        "secret": os.environ["WEBHOOK_SECRET"],
    }


async def handler(event: dict, context: Any) -> dict:
    """Точка входа Cloud Function ``bot``."""
    # Клиенты привязаны к циклу событий: если рантайм сменил цикл, собираем заново.
    loop = asyncio.get_running_loop()
    if _runtime.get("loop") is not loop:
        async with _lock():
            if _runtime.get("loop") is not loop:
                if _runtime:
                    log.warning("runtime: event loop changed, rebuilding clients", extra={"user_id": None, "error_kind": "loop_changed"})
                    old = dict(_runtime)
                    _runtime.clear()
                    await _close_runtime(old)
                _runtime.update(await _init_runtime())
    rt = _runtime
    return await process_webhook(
        event, rt["dp"], rt["bot"], rt["storage"], rt["secret"], datetime.now(timezone.utc)
    )
