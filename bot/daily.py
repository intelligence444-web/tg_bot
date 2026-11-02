"""Функция daily (таймер 09:00 МСК): напоминание о взвешивании (спец. 9.3, 11) и по
понедельникам — недельный отчёт (v1.1 10) с советом модели; лимит ИИ отчёт не тратит."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
ADVICE_TOTAL_BUDGET_S = 300.0  # общий срок на советы за запуск; дальше отчёты без совета
ADVICE_CONCURRENCY = 5  # советов одновременно
REPORT_WORKERS = 10  # отчётов готовится одновременно (чтение базы и совет)


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


async def _deactivate(storage: Storage, user_id: int, counts: dict[str, int]) -> None:
    log.info("user blocked the bot", extra={"user_id": user_id, "error_kind": "forbidden"})
    user = await storage.get_user(user_id)
    if user is not None:
        user.is_active = False
        await storage.save_user(user)
    counts["deactivated"] += 1


@dataclass
class _Run:
    storage: Storage
    bot: Bot
    today: date
    chat: VisionClient | None
    counts: dict[str, int]
    advice_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(ADVICE_CONCURRENCY))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    advice_deadline: float = 0.0  # время цикла событий, после которого советы не запрашиваются

    async def isolated(self, user_id: int, step: Callable[[int], Awaitable[None]]) -> None:
        """Шаг рассылки для одного пользователя: 403 — деактивация, любая другая ошибка — в журнал."""
        try:
            try:
                await step(user_id)
            except _Blocked:
                await _deactivate(self.storage, user_id, self.counts)
        except Exception:
            log.exception("daily failed for user", extra={"user_id": user_id, "error_kind": "daily"})

    async def reminder(self, user_id: int) -> None:
        """Напоминание о взвешивании: последнее взвешивание старше 7 дней, сегодня ещё не слали."""
        user = await _fresh(self.storage, user_id)
        if user is None or user.last_weigh_reminder_date == self.today:
            return
        weighings = await self.storage.weights(user_id)
        if weighings and weighings[-1].day >= self.today - WEIGH_STALE:
            return
        if await _send(self.bot, user, texts.WEIGH_REMINDER):
            self.counts["reminders"] += 1
            await self._mark(user_id, "last_weigh_reminder_date")

    async def report(self, user_id: int) -> None:
        """Отчёт за прошлую неделю: профиль, советы включены, ≥ 3 дней с записями еды."""
        user = await _fresh(self.storage, user_id)
        if user is None or user.profile is None or user.norm is None or user.last_report_date == self.today:
            return
        start, end = weekly.week_bounds(self.today)
        meals = await self.storage.meals_between(user_id, start, end)
        if len({m.local_date for m in meals}) < weekly.MIN_DAYS:
            return
        stats = weekly.compute_week(
            meals,
            await self.storage.activities_between(user_id, start, end),
            await self.storage.weights(user_id),
            lambda day: effective_norm(user, day).kcal,
            start,
            self.today,
            meal_days=await self.storage.meal_days(user_id, self.today - STREAK_LOOKBACK),
        )
        advice = await self._advice(stats, user)
        async with self.send_lock:  # отчёты готовятся параллельно, а уходят по одному (лимит Telegram)
            # Совет идёт до минуты: /stop_tips за это время тоже учитываем.
            user = await _fresh(self.storage, user_id)
            if user is None or user.last_report_date == self.today:
                return
            if await _send(self.bot, user, weekly.format_weekly(stats, advice)):
                self.counts["reports"] += 1
                await self._mark(user_id, "last_report_date")

    async def _advice(self, stats: weekly.WeekStats, user: User) -> str | None:
        if self.chat is None:
            return None
        loop = asyncio.get_running_loop()
        async with self.advice_slots:
            left = self.advice_deadline - loop.time()
            if left <= 0:
                return None  # общий бюджет на советы исчерпан — отчёт без совета
            return await weekly.weekly_advice(self.chat, stats, user.profile, timeout=min(weekly.ADVICE_TIMEOUT_S, left))

    async def _mark(self, user_id: int, field_name: str) -> None:
        """Дата отправки — чтобы повторный запуск daily в тот же день не слал снова."""
        user = await self.storage.get_user(user_id)
        if user is not None:
            setattr(user, field_name, self.today)
            await self.storage.save_user(user)


async def run_daily(
    storage: Storage, bot: Bot, now: datetime, chat: VisionClient | None = None
) -> dict[str, int]:
    """Рассылки дня. В понедельник сначала все напоминания о взвешивании, затем отчёты.

    ``chat`` — клиент совета в недельном отчёте (None — без совета). Советы запрашиваются
    не больше ``ADVICE_CONCURRENCY`` одновременно и в пределах ``ADVICE_TOTAL_BUDGET_S`` секунд
    от начала отчётов; дальше отчёты уходят без совета. Лимит ИИ пользователей не тратится.
    """
    today = to_local(now).date()
    counts = {"reminders": 0, "deactivated": 0, "reports": 0}
    if today.weekday() != MONDAY:
        return counts
    run = _Run(storage, bot, today, chat, counts)
    user_ids = [u.user_id for u in await storage.daily_users()]
    for uid in user_ids:
        await run.isolated(uid, run.reminder)

    run.advice_deadline = asyncio.get_running_loop().time() + ADVICE_TOTAL_BUDGET_S
    queue: asyncio.Queue[int] = asyncio.Queue()
    for uid in user_ids:
        queue.put_nowait(uid)

    async def worker() -> None:
        while not queue.empty():
            await run.isolated(queue.get_nowait(), run.report)

    await asyncio.gather(*(worker() for _ in range(REPORT_WORKERS)))
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
