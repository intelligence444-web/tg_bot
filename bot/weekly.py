"""Недельный отчёт (v1.1 10): цифры считает код, модель даёт только короткий совет.

Отчёт уходит в понедельник из ``bot.daily`` за прошлую неделю пн–вс, лимит ИИ не тратит.
В лог не пишется ни текст совета, ни ответ модели.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from bot import texts
from bot.calc import format_num
from bot.models import Activity, Meal, Profile, Weighing
from bot.progress import streak, trend_weight
from bot.vision import VisionClient

log = logging.getLogger(__name__)

IN_NORM = 0.10  # день «в норме» — ±10% от нормы дня (с добавкой активности)
MIN_DAYS = 3  # отчёт — при стольких днях с записями за неделю
ADVICE_MAX = 300  # знаков в совете
ADVICE_TIMEOUT_S = 60.0
ADVICE_MAX_TOKENS = 400

ADVICE_SYSTEM = (
    "Ты — помощник по питанию в Telegram-боте, который считает калории. По итогам прошлой недели "
    "дай один короткий совет на новую неделю: одно-два предложения, не больше 250 знаков, простым "
    "текстом без Markdown и без эмодзи. Обращайся на «ты», тепло и по-дружески. Посыл бота — "
    "питание под цель без запретов и без голода: не запрещай, не упрекай и не стыди, предложи "
    "один мягкий конкретный шаг. Не используй слова «нельзя», «запрещено», «сорвался», «плохо». "
    "Цифры бери только из сообщения, норму не пересчитывай. Не ставь диагнозов, не советуй "
    "лекарства и БАДы. Ответь только текстом совета."
)

_GOALS = {"lose": "снизить вес", "maintain": "держать вес", "gain": "набрать вес"}
_QUOTES = "\"'«»“”"


@dataclass(frozen=True)
class WeekStats:
    days_logged: int
    avg_kcal: float
    avg_protein: float
    days_in_norm: int
    activity_credit: float
    week_change_kg: float | None
    streak: int


def week_bounds(today: date) -> tuple[date, date]:
    """Прошлая полная неделя пн–вс перед ``today``."""
    start = today - timedelta(days=today.weekday() + 7)
    return start, start + timedelta(days=6)


def compute_week(
    meals: list[Meal],
    activities: list[Activity],
    weighings: list[Weighing],
    norm_kcal: float | Callable[[date], float],
    week_start: date,
    today: date,
    *,
    meal_days: set[date] | None = None,
) -> WeekStats:
    """Итоги недели ``week_start``..``week_start + 6``.

    ``norm_kcal`` — норма дня без активности (число или функция дня, например ``effective_norm``);
    к ней добавляется кредит активности этого дня. Средние — по дням с записями еды.
    Серия — по ``meal_days`` (дни с записями за долгий срок), иначе по датам ``meals``.
    """
    week_end = week_start + timedelta(days=6)
    by_day: dict[date, list[float]] = {}
    for m in meals:
        if week_start <= m.local_date <= week_end:
            kp = by_day.setdefault(m.local_date, [0.0, 0.0])
            kp[0] += m.analysis.kcal
            kp[1] += m.analysis.protein
    credits: dict[date, float] = {}
    for a in activities:
        if week_start <= a.local_date <= week_end:
            credits[a.local_date] = credits.get(a.local_date, 0.0) + a.kcal_credit

    def norm_on(day: date) -> float:
        base = norm_kcal(day) if callable(norm_kcal) else norm_kcal
        return base + credits.get(day, 0.0)

    days = len(by_day)
    in_norm = 0
    for day, (kcal, _) in by_day.items():
        norm = norm_on(day)
        if norm > 0 and abs(kcal - norm) <= IN_NORM * norm:
            in_norm += 1

    change = None
    end_trend = trend_weight(weighings, week_end)
    before = trend_weight(weighings, week_start - timedelta(days=1))
    if end_trend is not None and before is not None:
        change = end_trend - before

    all_days = meal_days if meal_days is not None else {m.local_date for m in meals}
    return WeekStats(
        days_logged=days,
        avg_kcal=sum(k for k, _ in by_day.values()) / days if days else 0.0,
        avg_protein=sum(p for _, p in by_day.values()) / days if days else 0.0,
        days_in_norm=in_norm,
        activity_credit=sum(credits.values()),
        week_change_kg=change,
        streak=streak(all_days, today),
    )


def _signed(x: float, ndigits: int = 1) -> str:
    s = format_num(abs(x), ndigits)
    if s == "0":
        return s
    return ("+" if x > 0 else "−") + s


def format_weekly(stats: WeekStats, advice: str | None) -> str:
    """Отчёт простым текстом (без parse_mode)."""
    lines = [
        texts.WEEKLY_TITLE,
        "",
        texts.WEEKLY_DAYS.format(days=stats.days_logged),
        texts.WEEKLY_AVG.format(kcal=format_num(stats.avg_kcal), protein=format_num(stats.avg_protein)),
        texts.WEEKLY_IN_NORM.format(
            n=stats.days_in_norm, word=texts.plural(stats.days_in_norm, *texts.DAY_WORDS)
        ),
    ]
    if round(stats.activity_credit) > 0:
        lines.append(texts.WEEKLY_ACTIVITY.format(kcal=format_num(stats.activity_credit)))
    if stats.week_change_kg is not None:
        lines.append(texts.WEEKLY_WEIGHT.format(delta=_signed(stats.week_change_kg)))
    if stats.streak >= 2:
        lines.append(texts.WEEKLY_STREAK.format(n=stats.streak, word=texts.plural(stats.streak, *texts.DAY_WORDS)))
    if advice:
        lines.append("")
        lines.append(texts.WEEKLY_ADVICE.format(advice=advice))
    return "\n".join(lines)


def _advice_request(stats: WeekStats, profile: Profile) -> str:
    lines = [
        f"Цель: {_GOALS.get(profile.goal, profile.goal)}.",
        f"Дней с записями еды: {stats.days_logged} из 7.",
        f"В среднем за день: {format_num(stats.avg_kcal)} ккал, белок {format_num(stats.avg_protein)} г.",
        f"Дней в норме калорий (±10%): {stats.days_in_norm}.",
        f"Активность за неделю: +{format_num(stats.activity_credit)} ккал к норме.",
    ]
    if stats.week_change_kg is not None:
        lines.append(f"Тренд веса за неделю: {_signed(stats.week_change_kg)} кг.")
    else:
        lines.append("Тренд веса за неделю: нет данных.")
    lines.append(f"Серия дней с записями: {stats.streak}.")
    return "\n".join(lines)


def _clean(raw: str) -> str | None:
    text = " ".join(raw.split()).strip(_QUOTES + " ")
    if not text:
        return None
    if len(text) > ADVICE_MAX:
        text = text[: ADVICE_MAX - 1].rstrip() + "…"
    return text


async def weekly_advice(chat: VisionClient, stats: WeekStats, profile: Profile) -> str | None:
    """Один запрос к модели; совет до ``ADVICE_MAX`` знаков. Любая ошибка — None."""
    messages = [{"role": "user", "content": _advice_request(stats, profile)}]
    try:
        async with asyncio.timeout(ADVICE_TIMEOUT_S):
            raw = await chat.complete(ADVICE_SYSTEM, messages, ADVICE_MAX_TOKENS)
    except Exception as e:  # сбой модели или сети — отчёт уходит без совета
        log.warning("weekly advice failed: %s", type(e).__name__, extra={"error_kind": "weekly_advice"})
        return None
    return _clean(raw) if isinstance(raw, str) else None
