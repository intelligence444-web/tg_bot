"""Функция daily (таймер 09:00 МСК): курс «Знание дня» и напоминание о взвешивании (спец. 9.3, 11)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from bot import course, texts
from bot.diary import to_local
from bot.models import User
from bot.storage import Storage

log = logging.getLogger(__name__)

SEND_PAUSE_S = 0.04  # не чаще 25 сообщений в секунду
WEIGH_STALE = timedelta(days=7)
MONDAY = 0


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


async def run_daily(storage: Storage, bot: Bot, now: datetime) -> dict[str, int]:
    today = to_local(now).date()
    counts = {"cards": 0, "reminders": 0, "deactivated": 0}
    for user in await storage.daily_users():
        try:
            card = course.card_for(user.course_day + 1)
            if card is not None and user.onboarded_at is not None and user.onboarded_at < today:
                day = user.course_day + 1
                if await _send(bot, user, course.format_card(day, card), parse_mode="HTML"):
                    user.course_day = day
                    await storage.save_user(user)
                    counts["cards"] += 1
            if today.weekday() == MONDAY:
                weighings = await storage.weights(user.user_id)
                if not weighings or weighings[-1].day < today - WEIGH_STALE:
                    if await _send(bot, user, texts.WEIGH_REMINDER):
                        counts["reminders"] += 1
        except _Blocked:
            log.info("user blocked the bot", extra={"user_id": user.user_id, "error_kind": "forbidden"})
            user.is_active = False
            await storage.save_user(user)
            counts["deactivated"] += 1
    return counts


async def handler(event: dict, context: Any) -> dict:
    """Точка входа Cloud Function таймера."""
    from bot import ydb_storage  # noqa: PLC0415 — тесты не тянут YDB

    pool = await ydb_storage.connect(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"])
    bot = Bot(token=os.environ["BOT_TOKEN"])
    try:
        counts = await run_daily(ydb_storage.YdbStorage(pool), bot, datetime.now(timezone.utc))
    finally:
        await bot.session.close()
        await ydb_storage.close(pool)
    log.info("daily done: %s", counts)
    return {"statusCode": 200, "body": json.dumps(counts)}
