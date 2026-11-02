"""Функция daily (таймер 09:00 МСК): напоминание о взвешивании (спец. 9.3, 11) и по
понедельникам — недельный отчёт (v1.1 10) с советом модели; лимит ИИ отчёт не тратит."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from bot import texts, weekly
from bot.calc import effective_norm
from bot.diary import to_local
from bot.models import User
from bot.storage import Storage
from bot.vision import VisionClient

log = logging.getLogger(__name__)

SEND_PAUSE_S = 0.04  # не чаще 25 сообщений в секунду
WEIGH_STALE = timedelta(days=7)
MONDAY = 0
STREAK_LOOKBACK = timedelta(days=60)
CHAT_TIMEOUT_S = 45.0  # как VISION_TIMEOUT_S в bot.main


class _Blocked(Exception):
    """Пользователь заблокировал бота."""


async def _send(bot: Bot, user: User, text: str, **kwargs: Any) -> bool:
    """True — отправлено; False — ошибка Telegram (записана в журнал); _Blocked — 403."""
    try:
        await bot.send_message(user.user_id, text, **kwargs)
    except TelegramForbiddenError as e:
        raise _Blocked from e
    except TelegramAPIError as e:
        log.warning("daily send failed: %s", type(e).__name__, extra={"user_id": user.user_id, "error_kind": "telegram"})
        return False
    finally:
        await asyncio.sleep(SEND_PAUSE_S)
    return True


async def _fresh(storage: Storage, user_id: int) -> User | None:
    """Свежая копия пользователя прямо перед отправкой: рассылка идёт долго, а /stop_tips
    или правка профиля могли случиться после ``daily_users()``."""
    user = await storage.get_user(user_id)
    if user is None or not user.is_active or not user.tips_enabled:
        return None
    return user


async def _weekly_report(
    storage: Storage, bot: Bot, user_id: int, today: date, chat: VisionClient | None, counts: dict[str, int]
) -> None:
    """Отчёт за прошлую неделю: профиль, советы включены, ≥ 3 дней с записями еды."""
    user = await _fresh(storage, user_id)
    if user is None or user.profile is None or user.norm is None:
        return
    start, end = weekly.week_bounds(today)
    meals = await storage.meals_between(user_id, start, end)
    if len({m.local_date for m in meals}) < weekly.MIN_DAYS:
        return
    stats = weekly.compute_week(
        meals,
        await storage.activities_between(user_id, start, end),
        await storage.weights(user_id),
        lambda day: effective_norm(user, day).kcal,
        start,
        today,
        meal_days=await storage.meal_days(user_id, today - STREAK_LOOKBACK),
    )
    advice = await weekly.weekly_advice(chat, stats, user.profile) if chat is not None else None
    # Совет идёт до минуты: /stop_tips за это время тоже учитываем.
    user = await _fresh(storage, user_id)
    if user is not None and await _send(bot, user, weekly.format_weekly(stats, advice)):
        counts["reports"] += 1


async def _process(
    storage: Storage,
    bot: Bot,
    user_id: int,
    today: date,
    counts: dict[str, int],
    chat: VisionClient | None = None,
) -> None:
    user = await _fresh(storage, user_id)
    try:
        if user is None:
            return
        if today.weekday() == MONDAY:
            weighings = await storage.weights(user_id)
            if not weighings or weighings[-1].day < today - WEIGH_STALE:
                user = await _fresh(storage, user_id)
                if user is not None and await _send(bot, user, texts.WEIGH_REMINDER):
                    counts["reminders"] += 1
            await _weekly_report(storage, bot, user_id, today, chat, counts)
    except _Blocked:
        log.info("user blocked the bot", extra={"user_id": user_id, "error_kind": "forbidden"})
        user = await storage.get_user(user_id)
        if user is not None:
            user.is_active = False
            await storage.save_user(user)
        counts["deactivated"] += 1


async def run_daily(
    storage: Storage, bot: Bot, now: datetime, chat: VisionClient | None = None
) -> dict[str, int]:
    """``chat`` — клиент для совета в недельном отчёте; None — отчёт без совета."""
    today = to_local(now).date()
    counts = {"reminders": 0, "deactivated": 0, "reports": 0}
    for candidate in await storage.daily_users():
        uid = candidate.user_id
        try:
            await _process(storage, bot, uid, today, counts, chat)
        except Exception:
            log.exception("daily failed for user", extra={"user_id": uid, "error_kind": "daily"})
    return counts


def build_chat(http: httpx.AsyncClient) -> VisionClient | None:
    """Клиент совета: ``CHAT_PRIMARY``/``CHAT_FALLBACK``, иначе ``VISION_PRIMARY``/``VISION_FALLBACK``.

    Нет ``KIE_API_KEY`` или модели (или модель задана неверно) — WARNING и отчёты без совета.
    """
    from bot.vision import build_vision  # noqa: PLC0415

    api_key = os.environ.get("KIE_API_KEY")
    primary = os.environ.get("CHAT_PRIMARY") or os.environ.get("VISION_PRIMARY")
    fallback = os.environ.get("CHAT_FALLBACK") or os.environ.get("VISION_FALLBACK") or None
    if not api_key or not primary:
        log.warning("daily: no KIE_API_KEY or chat model, weekly reports without advice", extra={"user_id": None, "error_kind": "config"})
        return None
    try:
        return build_vision(primary, fallback, api_key, http)
    except ValueError:
        log.warning("daily: bad chat model spec, weekly reports without advice", extra={"user_id": None, "error_kind": "config"})
        return None


async def handler(event: dict, context: Any) -> dict:
    """Точка входа Cloud Function таймера."""
    from bot import ydb_storage  # noqa: PLC0415 — тесты не тянут YDB

    pool = await ydb_storage.connect(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"])
    bot = Bot(token=os.environ["BOT_TOKEN"])
    http = httpx.AsyncClient(timeout=CHAT_TIMEOUT_S)
    try:
        counts = await run_daily(ydb_storage.YdbStorage(pool), bot, datetime.now(timezone.utc), build_chat(http))
    finally:
        await http.aclose()
        await bot.session.close()
        await ydb_storage.close(pool)
    log.info("daily done: %s", counts)
    return {"statusCode": 200, "body": json.dumps(counts)}
