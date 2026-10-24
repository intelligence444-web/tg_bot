import pytest

from bot.calc import (
    bmi,
    bmr,
    calculate_norm,
    format_num,
    lose_allowed,
    parse_number,
    planned_pace_kg_week,
    planned_weeks_to_goal,
    round_half_up,
    target_bmi_low,
)
from bot.models import Profile

P = Profile("female", 30, 165, 70, "moderate", "lose", 62)


def test_reference_example():
    n = calculate_norm(P)
    assert n.bmr == pytest.approx(1420.25)
    assert n.tdee == pytest.approx(2201.3875)
    assert n.kcal == pytest.approx(1761.11)
    assert round_half_up(n.protein_g) == 126 and round_half_up(n.fat_g) == 63
    assert round_half_up(n.carbs_g) == 173
    assert n.water_ml == pytest.approx(2100)
    assert round_half_up(planned_pace_kg_week(n), 1) == 0.4
    assert round_half_up(planned_weeks_to_goal(P, n)) == 20


def test_male_bmr():
    assert bmr("male", 80, 180, 40) == pytest.approx(1730)


@pytest.mark.parametrize(
    "activity,f",
    [("sedentary", 1.2), ("light", 1.375), ("moderate", 1.55), ("high", 1.725), ("very_high", 1.9)],
)
def test_activity_factors(activity, f):
    p = Profile("male", 40, 180, 80, activity, "maintain", None)
    n = calculate_norm(p)
    assert n.tdee == pytest.approx(n.bmr * f)


def test_lose_never_below_bmr():
    p = Profile("female", 30, 165, 70, "sedentary", "lose", 60)
    n = calculate_norm(p)
    assert n.tdee * 0.8 < n.bmr
    assert n.kcal == pytest.approx(n.bmr)


def test_gain_plus_10_percent():
    p = Profile("male", 25, 180, 70, "light", "gain", 75)
    n = calculate_norm(p)
    assert n.kcal == pytest.approx(n.tdee * 1.1)
    assert n.protein_g == pytest.approx(1.6 * 70)


def test_maintain():
    p = Profile("male", 25, 180, 70, "light", "maintain", None)
    n = calculate_norm(p)
    assert n.kcal == pytest.approx(n.tdee)
    assert n.protein_g == pytest.approx(1.4 * 70)
    assert planned_weeks_to_goal(p, n) is None


def test_carbs_not_negative():
    p = Profile("female", 30, 165, 70, "sedentary", "maintain", None)
    n = calculate_norm(p, 500)
    assert n.carbs_g == 0


def test_kcal_override():
    n = calculate_norm(P, 2000)
    assert n.kcal == 2000
    assert n.carbs_g == pytest.approx((2000 - n.protein_g * 4 - n.fat_g * 9) / 4)


def test_lose_allowed():
    assert lose_allowed(14) is False
    assert lose_allowed(15) is False
    assert lose_allowed(16) is True


def test_target_bmi_low():
    assert bmi(50, 165) == pytest.approx(18.37, abs=0.01)
    assert target_bmi_low(50, 165) is True
    assert target_bmi_low(51, 165) is False


def test_round_half_up():
    assert round_half_up(172.5) == 173
    assert round_half_up(0.45, 1) == 0.5


def test_format_num():
    assert format_num(1761.11) == "1 761"
    assert format_num(0.4, 1) == "0,4"
    assert format_num(2.1, 1) == "2,1"
    assert format_num(1.0, 1) == "1"


@pytest.mark.parametrize(
    "text,expected",
    [("70", 70), ("70,5 кг", 70.5), (" 165 см", 165), ("70.5", 70.5), ("абв", None), ("500", None), ("", None)],
)
def test_parse_number(text, expected):
    assert parse_number(text, 30, 300) == expected
