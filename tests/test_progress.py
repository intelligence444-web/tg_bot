from datetime import date, timedelta

import pytest

from bot.models import Weighing
from bot.progress import (
    Drift,
    MILESTONE_ORDER,
    actual_pace,
    drift,
    forecast_date,
    new_milestones,
    passed_kg,
    progress_percent,
    streak,
    trend_series,
    trend_weight,
)


def W(d, kg):
    return Weighing(date(2026, 11, d), kg)


def D(d):
    return date(2026, 11, d)


def test_trend_window_average():
    ws = [W(1, 70), W(5, 69), W(8, 68)]
    assert trend_weight(ws, D(8)) == pytest.approx(68.5)


def test_trend_falls_back_to_last():
    assert trend_weight([W(1, 70)], D(20)) == 70


def test_trend_none_without_data():
    assert trend_weight([], D(1)) is None


def test_trend_ignores_future():
    assert trend_weight([W(10, 60)], D(5)) is None


def test_trend_series():
    ws = [W(1, 70), W(5, 69), W(8, 68)]
    assert trend_series(ws) == [(D(1), 70), (D(5), 69.5), (D(8), pytest.approx(68.5))]


def test_percent():
    assert passed_kg(70, 66.8, 62) == pytest.approx(3.2)
    assert progress_percent(70, 66.8, 62) == pytest.approx(40)


def test_percent_moving_away_is_zero():
    assert passed_kg(70, 71, 62) == 0
    assert progress_percent(70, 71, 62) == 0


def test_percent_capped():
    assert progress_percent(70, 61, 62) == 100


def test_percent_gain():
    assert progress_percent(60, 62, 68) == pytest.approx(25)


def test_actual_pace_needs_7_days():
    assert actual_pace([W(1, 70), W(4, 69)], D(5)) is None


def test_actual_pace_regression():
    assert actual_pace([W(1, 70), W(15, 69.3)], D(15)) == pytest.approx(-0.35)


def test_forecast():
    assert forecast_date(66, 62, -0.5, date(2026, 11, 1)) == date(2026, 12, 27)


def test_forecast_wrong_direction():
    assert forecast_date(66, 62, 0.2, D(1)) is None
    assert forecast_date(66, 62, 0, D(1)) is None


def test_milestones_several_at_once():
    assert new_milestones(70, 66.5, 62, "lose", set()) == ["first_kg", "five_percent", "q25"]
    assert new_milestones(70, 67.5, 62, "lose", set()) == ["first_kg", "q25"]


def test_milestones_not_repeated():
    assert "first_kg" not in new_milestones(70, 66.5, 62, "lose", {"first_kg"})


def test_five_percent_only_lose():
    assert "five_percent" not in new_milestones(60, 64, 68, "gain", set())


def test_goal_milestone():
    assert "goal" in new_milestones(70, 61.9, 62, "lose", set())
    assert MILESTONE_ORDER[-1] == "goal"


def test_streak_today_logged():
    assert streak({D(3), D(4), D(5)}, D(5)) == 3


def test_streak_today_not_logged():
    assert streak({D(3), D(4)}, D(5)) == 2


def test_streak_broken():
    assert streak({D(1), D(3), D(4)}, D(4)) == 2


def test_drift_weekly_up():
    ws = [W(1, 68.0), W(8, 68.4)]
    # тренд на 1.11 = 68.0; на 8.11 = среднее окна 2..8 = 68.4
    assert drift(ws, D(8), 62, "lose").weekly_up is True


def test_drift_small_noise():
    ws = [W(1, 68.0), W(8, 68.3)]
    assert drift(ws, D(8), 62, "lose") == Drift(False, False)


def test_drift_three_weeks():
    ws = [W(1, 66.0), W(8, 66.5), W(15, 67.0), W(22, 67.5)]
    assert drift(ws, D(22), 62, "lose").three_weeks is True


def test_drift_three_weeks_needs_history():
    ws = [W(15, 67.0), W(22, 67.5)]
    assert drift(ws, D(22), 62, "lose").three_weeks is False


def test_drift_gain_and_maintain():
    ws = [W(1, 60.0), W(8, 59.5)]
    assert drift(ws, D(8), 68, "gain").weekly_up is True
    ws = [W(1, 65.0), W(8, 64.5)]
    assert drift(ws, D(8), 65, "maintain").weekly_up is True


def test_forecast_too_far_is_none():
    assert forecast_date(66, 62, -1e-9, D(1)) is None
    # 4 кг при 0,02 кг/нед = 200 недель > 3 лет
    assert forecast_date(66, 62, -0.02, D(1)) is None
    # 4 кг при 0,04 кг/нед = 100 недель = 700 дней — показываем
    assert forecast_date(66, 62, -0.04, D(1)) == D(1) + timedelta(days=700)
