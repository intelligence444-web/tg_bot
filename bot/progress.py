"""Чистые расчёты прогресса веса (спец. разделы 8 и 9)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from bot.models import Goal, Weighing

_EPS = 1e-9
_DRIFT_KG = 0.3
_MAX_FORECAST_DAYS = 1096  # дальше трёх лет прогноз не показываем

MILESTONE_ORDER = ("first_kg", "five_percent", "q25", "q50", "q75", "goal")


def trend_weight(weighings: list[Weighing], on: date) -> float | None:
    """Среднее за 7 дней, заканчивающихся `on`; иначе последнее взвешивание до `on`."""
    start = on - timedelta(days=6)
    window = [w.weight_kg for w in weighings if start <= w.day <= on]
    if window:
        return sum(window) / len(window)
    earlier = [w for w in weighings if w.day <= on]
    if not earlier:
        return None
    return max(earlier, key=lambda w: w.day).weight_kg


def trend_series(weighings: list[Weighing]) -> list[tuple[date, float]]:
    """Вес тренда на дату каждого взвешивания."""
    result = []
    for day in sorted({w.day for w in weighings}):
        value = trend_weight(weighings, day)
        assert value is not None
        result.append((day, value))
    return result


def passed_kg(start: float, trend: float, target: float) -> float:
    """Пройдено кг в сторону цели; движение от цели даёт 0."""
    if target == start:
        return 0.0
    direction = 1.0 if target > start else -1.0
    return max(0.0, (trend - start) * direction)


def progress_percent(start: float, trend: float, target: float) -> float:
    total = abs(start - target)
    if total == 0:
        return 0.0
    return min(100.0, max(0.0, passed_kg(start, trend, target) / total * 100))


def actual_pace(weighings: list[Weighing], today: date) -> float | None:
    """Наклон МНК за 28 дней до today включительно, кг/нед; None, если данных мало."""
    since = today - timedelta(days=28)
    pts = sorted((w for w in weighings if since <= w.day <= today), key=lambda w: w.day)
    if len(pts) < 2 or (pts[-1].day - pts[0].day).days < 7:
        return None
    x0 = pts[0].day
    xs = [(w.day - x0).days for w in pts]
    ys = [w.weight_kg for w in pts]
    n = len(pts)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx * 7


def forecast_date(trend: float, target: float, pace: float, today: date) -> date | None:
    """Дата достижения цели; None, если темп 0, направлен не к цели или цель дальше трёх лет."""
    if pace == 0:
        return None
    needed = target - trend
    if needed * pace < 0:
        return None
    days = abs(needed) / abs(pace) * 7
    if days > _MAX_FORECAST_DAYS:
        return None
    return today + timedelta(days=round(days))


def new_milestones(
    start: float, trend: float, target: float, goal: Goal, sent: set[str]
) -> list[str]:
    passed = passed_kg(start, trend, target)
    percent = progress_percent(start, trend, target)
    reached = {
        "first_kg": passed >= 1 - _EPS,
        "five_percent": goal == "lose" and passed >= 0.05 * start - _EPS,
        "q25": percent >= 25 - _EPS,
        "q50": percent >= 50 - _EPS,
        "q75": percent >= 75 - _EPS,
        "goal": percent >= 100 - _EPS,
    }
    return [m for m in MILESTONE_ORDER if reached[m] and m not in sent]


def streak(meal_days: set[date], today: date) -> int:
    day = today if today in meal_days else today - timedelta(days=1)
    count = 0
    while day in meal_days:
        count += 1
        day -= timedelta(days=1)
    return count


@dataclass(frozen=True)
class Drift:
    weekly_up: bool
    three_weeks: bool


def _away(goal: Goal, target: float, now: float, before: float) -> float:
    """Насколько `now` дальше от цели, чем `before` (кг; отрицательно — ближе)."""
    if goal == "lose":
        return now - before
    if goal == "gain":
        return before - now
    return abs(now - target) - abs(before - target)


def drift(weighings: list[Weighing], today: date, target: float, goal: Goal) -> Drift:
    now = trend_weight(weighings, today)
    if now is None:
        return Drift(False, False)
    past = [trend_weight(weighings, today - timedelta(days=n)) for n in (7, 14, 21)]
    weekly_up = past[0] is not None and _away(goal, target, now, past[0]) > _DRIFT_KG + _EPS
    three_weeks = all(
        p is not None and _away(goal, target, now, p) > _EPS for p in past
    )
    return Drift(weekly_up, three_weeks)
