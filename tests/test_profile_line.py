from datetime import date

import pytest

from bot.profile_line import ParsedProfile, parse_profile_line, parse_steps, parse_weight_text

T = date(2026, 11, 2)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("муж, 1987, 180, 87", ParsedProfile("male", 39, 180, 87)),
        ("Ж 35 165 60,5", ParsedProfile("female", 35, 165, 60.5)),
        ("женщина; 1990 / 170 см / 65 кг", ParsedProfile("female", 36, 170, 65)),
        ("m 2012 160 50", ParsedProfile("male", 14, 160, 50)),
        ("m 2013 160 50", "partial"),
        ("муж 1987", "partial"),
        ("муж 1910 180 80", "partial"),
        ("муж 35 100 80", "partial"),
        ("муж 35 180 20", "partial"),
        ("съел 2 яйца", None),
        ("85", None),
    ],
)
def test_parse_profile_line(text, expected):
    assert parse_profile_line(text, T) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("85", 85), ("вес 85,5", 85.5), ("85 кг", 85), ("Вес85", 85), ("850", None), ("съел 85 г", None), ("20", None)],
)
def test_parse_weight_text(text, expected):
    assert parse_weight_text(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("прошёл 12 000 шагов", 12000),
        ("прошёл 12 000 шагов", 12000),
        ("12000 шагов", 12000),
        ("шагов 8000", 8000),
        ("300 шагов", None),
        ("шагов много", None),
        ("200000 шагов", None),
    ],
)
def test_parse_steps(text, expected):
    assert parse_steps(text) == expected
