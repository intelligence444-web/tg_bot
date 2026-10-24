from datetime import date, datetime, timezone

import pytest

from bot import texts
from bot.calc import Profile, calculate_norm
from bot.diary import DaySummary, format_day, meal_type_for, summarize, to_local
from bot.models import Analysis, FoodItem, Meal

NORM = calculate_norm(Profile("female", 30, 165, 70, "moderate", "lose", 62))


@pytest.mark.parametrize("hm,expected", [("04:59", "snack"), ("05:00", "breakfast"), ("10:59", "breakfast"),
    ("11:00", "lunch"), ("15:59", "lunch"), ("16:00", "snack"), ("16:59", "snack"), ("17:00", "dinner"),
    ("22:59", "dinner"), ("23:00", "snack")])
def test_meal_type_boundaries(hm, expected):
    h, m = map(int, hm.split(":"))
    assert meal_type_for(datetime(2026, 11, 1, h, m)) == expected


def test_to_local_midnight():
    a = to_local(datetime(2026, 11, 1, 20, 30, tzinfo=timezone.utc))
    assert (a.day, a.hour, a.minute) == (1, 23, 30)
    b = to_local(datetime(2026, 11, 1, 21, 30, tzinfo=timezone.utc))
    assert (b.day, b.hour, b.minute) == (2, 0, 30)


def meal(mt, kcal, p=0.0, f=0.0, c=0.0):
    it = FoodItem("x", 100, kcal, p, f, c, False, "main")
    return Meal(1, f"{mt}{kcal}", datetime(2026, 11, 1, 9), date(2026, 11, 1), mt, Analysis(True, "high", (it,), False))


def ref_summary():
    return summarize([
        meal("breakfast", 380, 30, 10, 40),
        meal("lunch", 545, 40, 20, 50),
        meal("snack", 160, 14, 5, 20),
        meal("snack", 160, 14, 6, 18),
    ])


def test_summarize():
    s = ref_summary()
    assert s.kcal == 1245 and s.protein == 98 and s.fat == 41 and s.carbs == 128
    assert s.by_type == {"breakfast": 380, "lunch": 545, "snack": 320}


def test_format_day_reference():
    text = format_day(ref_summary(), NORM, 5)
    assert text == (
        "📅 Сегодня — 1 245 / 1 761 ккал  ▓▓▓▓▓▓▓░░░ 71%\n"
        "🔥 5 дней подряд\n"
        "\n"
        "🌅 Завтрак — 380 ккал\n"
        "🍽 Обед — 545 ккал\n"
        "🍎 Перекус — 320 ккал\n"
        "🌙 Ужин — пока пусто · можно ещё 516 ккал\n"
        "\n"
        "💪 Белок 98 / 126 г — главное для сытости\n"
        "Жиры 41 / 63 г · Углеводы 128 / 173 г"
    )
    assert text.index("💪 Белок") < text.index("Жиры")


def test_format_day_no_streak_line_below_2():
    for n in (0, 1):
        assert "подряд" not in format_day(ref_summary(), NORM, n)
    text = format_day(ref_summary(), NORM, 1)
    assert text.splitlines()[1] == ""


def test_format_day_over_norm():
    s = DaySummary(2000, 100, 60, 200, {"lunch": 2000})
    text = format_day(s, NORM, 0)
    assert "▓▓▓▓▓▓▓▓▓▓ 114%" in text
    assert "пока пусто · можно ещё 0 ккал" in text
    assert "🍽 Обед — 2 000 ккал" in text


def test_empty_day():
    text = format_day(DaySummary(0, 0, 0, 0, {}), NORM, 0)
    assert "░░░░░░░░░░ 0%" in text
    assert "🌅 Завтрак — пока пусто · можно ещё 1 761 ккал" in text


def test_streak_word_forms():
    assert "2 дня подряд" in format_day(ref_summary(), NORM, 2)
    assert "5 дней подряд" in format_day(ref_summary(), NORM, 5)
    assert "21 день подряд" in format_day(ref_summary(), NORM, 21)
    assert texts.plural(11, "день", "дня", "дней") == "дней"
    assert texts.plural(112, "день", "дня", "дней") == "дней"
