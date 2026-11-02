"""Недельный отчёт (v1.1 10): расчёт, текст, совет модели."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bot import texts
from bot.models import Activity, Analysis, FoodItem, Meal, Profile, Weighing
from bot.vision import VisionUnavailable
from bot.weekly import ADVICE_MAX, WeekStats, compute_week, format_weekly, week_bounds, weekly_advice
from tests.fakes import FakeVision

MON = date(2026, 10, 26)
WEEK_START = date(2026, 10, 19)
PROFILE = Profile("female", 30, 165.0, 70.0, "moderate", "lose", 62.0)


def meal(day: date, kcal: float, protein: float = 0.0) -> Meal:
    item = FoodItem("Еда", 100, kcal, protein, 0.0, 0.0, False, "main")
    return Meal(
        user_id=1,
        meal_id=f"{day.isoformat()}-{kcal}",
        eaten_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc),
        local_date=day,
        meal_type="lunch",
        analysis=Analysis(True, "high", (item,), False),
    )


def activity(day: date, credit: float) -> Activity:
    return Activity(1, f"a-{day}", day, "бег", credit * 2, credit, datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc))


def d(day: int) -> date:
    return date(2026, 10, day)


DATA_MEALS = [
    meal(d(18), 5000, 10),  # прошлая неделя — не считается
    meal(d(21), 1200, 60), meal(d(21), 800, 40),  # 2000 — в норме
    meal(d(22), 2100, 80),  # +5% — в норме
    meal(d(23), 1500, 60),  # −25% — нет
    meal(d(24), 2500, 90),  # норма с активностью 2400 — в норме
    meal(d(25), 2300, 70),  # +15% — нет
    meal(d(26), 900, 30),  # сегодня — не считается
]
DATA_ACTIVITIES = [activity(d(24), 400), activity(d(26), 100)]
DATA_WEIGHINGS = [Weighing(d(18), 71.0), Weighing(d(25), 70.0)]


def test_week_bounds_previous_mon_to_sun():
    assert week_bounds(MON) == (WEEK_START, d(25))
    assert week_bounds(d(28)) == (WEEK_START, d(25))


def test_compute_week():
    stats = compute_week(DATA_MEALS, DATA_ACTIVITIES, DATA_WEIGHINGS, 2000.0, WEEK_START, MON)
    assert stats.days_logged == 5
    assert stats.avg_kcal == pytest.approx((2000 + 2100 + 1500 + 2500 + 2300) / 5)
    assert stats.avg_protein == pytest.approx((100 + 80 + 60 + 90 + 70) / 5)
    assert stats.days_in_norm == 3
    assert stats.activity_credit == pytest.approx(400)
    assert stats.week_change_kg == pytest.approx(-1.0)
    assert stats.streak == 6  # 21–25 и сегодня
    assert stats.avg_norm == pytest.approx((2000 * 4 + 2400) / 5)


def test_week_change_uses_only_weighings_inside_both_windows():
    def change(weighings):
        return compute_week(DATA_MEALS, [], weighings, 2000.0, WEEK_START, MON).week_change_kg

    # среднее недели 19–25 минус среднее недели 12–18
    assert change([Weighing(d(12), 72.0), Weighing(d(18), 71.0), Weighing(d(20), 70.5), Weighing(d(25), 69.5)]) == (
        pytest.approx(70.0 - 71.5)
    )
    # на неделе отчёта взвешиваний нет — старое не подставляется
    assert change([Weighing(d(12), 72.0), Weighing(d(18), 71.0)]) is None
    # в базовой неделе нет, есть только более старое взвешивание
    assert change([Weighing(d(5), 72.0), Weighing(d(25), 70.0)]) is None
    # взвешивание сегодня (после недели) не считается
    assert change([Weighing(d(18), 71.0), Weighing(d(26), 69.0)]) is None


def test_compute_week_norm_per_day_and_meal_days_for_streak():
    def norm(day: date) -> float:
        return 2500.0 if day == d(23) else 2000.0

    days = {d(n) for n in range(15, 26)}
    stats = compute_week(DATA_MEALS, [], [], norm, WEEK_START, MON, meal_days=days)
    # 21, 22 в норме; 24 без активности: 2500 при норме 2000 — нет; 23: 1500 при 2500 — нет
    assert stats.days_in_norm == 2
    assert stats.activity_credit == 0
    assert stats.week_change_kg is None
    assert stats.streak == 11


def test_compute_week_empty():
    stats = compute_week([], [], [], 2000.0, WEEK_START, MON)
    assert stats == WeekStats(0, 0.0, 0.0, 0, 0.0, None, 0, None)


STATS = WeekStats(5, 2080.0, 80.0, 3, 400.0, -1.0, 5)


def test_format_weekly_with_advice():
    out = format_weekly(STATS, "Добавь овощей к ужину.")
    assert out.startswith(texts.WEEKLY_TITLE)
    assert "5 из 7" in out
    assert "2 080 ккал" in out and "белок 80 г" in out
    assert "3 дня" in out
    assert "+400 ккал" in out
    assert "−1 кг" in out
    assert "5 дней" in out
    assert out.endswith(texts.WEEKLY_ADVICE.format(advice="Добавь овощей к ужину."))


def test_format_weekly_without_optional_lines():
    out = format_weekly(WeekStats(3, 1900.0, 70.0, 0, 0.0, None, 1), None)
    assert "Совет" not in out
    assert "Активность" not in out
    assert "Вес" not in out
    assert "подряд" not in out
    for bad in ("нельзя", "запрещено", "сорвался", "плохо"):
        assert bad not in out.lower()


async def test_weekly_advice_one_call_with_numbers():
    chat = FakeVision([], texts=["  Добавь белка к завтраку.  "])
    assert await weekly_advice(chat, STATS, PROFILE) == "Добавь белка к завтраку."
    assert len(chat.completions) == 1
    call = chat.completions[0]
    assert "2 080" in call["messages"][-1]["content"]
    assert "нельзя" in call["system"]  # правило тона: не использовать эти слова
    assert "не советуй есть меньше нормы" in call["system"].lower()
    assert "добрать до нормы, лучше белком" in call["system"]


@pytest.mark.parametrize(
    ("avg_kcal", "expected"),
    [
        (1500.0, "ниже нормы на 500 ккал (25%)"),
        (2200.0, "выше нормы на 200 ккал (10%)"),
        (2000.0, "ровно по норме"),
    ],
)
async def test_weekly_advice_reports_average_norm_and_gap(avg_kcal, expected):
    chat = FakeVision([], texts=["Совет."])
    stats = WeekStats(5, avg_kcal, 80.0, 3, 0.0, None, 5, avg_norm=2000.0)
    await weekly_advice(chat, stats, PROFILE)
    content = chat.completions[0]["messages"][-1]["content"]
    assert "Средняя норма дня с учётом активности: 2 000 ккал." in content
    assert expected in content


async def test_weekly_advice_cut_to_300():
    chat = FakeVision([], texts=["а" * 1000])
    advice = await weekly_advice(chat, STATS, PROFILE)
    assert advice is not None and len(advice) <= ADVICE_MAX


@pytest.mark.parametrize("reply", [VisionUnavailable("down"), RuntimeError("boom"), "   "])
async def test_weekly_advice_failure_is_none(reply):
    chat = FakeVision([], texts=[reply])
    assert await weekly_advice(chat, STATS, PROFILE) is None


async def test_weekly_advice_strips_json_like_wrappers():
    chat = FakeVision([], texts=['"Пей воду перед едой."'])
    assert await weekly_advice(chat, STATS, PROFILE) == "Пей воду перед едой."


def test_week_start_sanity():
    assert WEEK_START.weekday() == 0 and WEEK_START + timedelta(days=7) == MON


def test_advice_prompt_minor_and_eating_disorder_rule():
    from bot.weekly import ADVICE_SYSTEM

    assert "меньше 18 лет" in ADVICE_SYSTEM
    assert "признаки расстройства пищевого поведения" in ADVICE_SYSTEM
    line = next(s for s in ADVICE_SYSTEM.split(". ") if "меньше 18 лет" in s)
    assert "дефицит" in line and "снижени" in line


async def test_advice_request_has_age():
    chat = FakeVision([], texts=["Совет"])
    stats = WeekStats(5, 1800.0, 90.0, 3, 0.0, None, 5, 1900.0)
    await weekly_advice(chat, stats, PROFILE)
    assert "Возраст: 30." in chat.completions[0]["messages"][-1]["content"]
