from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from bot.models import Sex

_MALE = {"м", "муж", "мужчина", "мужской", "m", "male", "man"}
_FEMALE = {"ж", "жен", "женщина", "женский", "f", "female", "w", "woman"}
_UNITS = {"см", "кг", "лет", "год", "года", "г", "cm", "kg", "y", "yo"}

_WORD = re.compile(r"[a-zа-яё]+")
# Десятичная запятая — только с одной цифрой после неё: «60,5», но не «1987,180».
_NUM = re.compile(r"\d+(?:[.,]\d)?(?!\d)")

_MIN_AGE, _MAX_AGE = 14, 100
_MIN_YEAR = 1920
_YEAR_THRESHOLD = 1900


@dataclass(frozen=True)
class ParsedProfile:
    sex: Sex
    age: int
    height_cm: float
    weight_kg: float


def _age_from(value: float, today: date) -> int | None:
    if value != int(value):
        return None
    n = int(value)
    if n >= _YEAR_THRESHOLD:
        if _MIN_YEAR <= n <= today.year - _MIN_AGE:
            return today.year - n
        return None
    return n if _MIN_AGE <= n <= _MAX_AGE else None


def parse_profile_line(text: str, today: date) -> ParsedProfile | Literal["partial"] | None:
    low = text.lower()
    sex: Sex | None = None
    for w in _WORD.findall(low):
        if w in _MALE:
            sex = "male"
        elif w in _FEMALE:
            sex = "female"
        elif w not in _UNITS:
            return None
    if sex is None:
        return None
    nums = [float(m.replace(",", ".")) for m in _NUM.findall(low)]
    if not nums:
        return None
    if len(nums) != 3:
        return "partial"
    raw_age, height, weight = nums
    age = _age_from(raw_age, today)
    if age is None or not 120 <= height <= 230 or not 30 <= weight <= 300:
        return "partial"
    return ParsedProfile(sex, age, height, weight)


_WEIGHT_RE = re.compile(r"(вес\s*)?(\d{2,3}(?:[.,]\d)?)\s*(кг)?", re.IGNORECASE)
_STEPS_BEFORE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[   ]\d{3})+|\d+)\s*шаг", re.IGNORECASE)
_STEPS_AFTER = re.compile(r"шаг\w*[\s:=-]*(\d{1,3}(?:[   ]\d{3})+|\d+)(?![\d.,])", re.IGNORECASE)


def parse_weight_text(text: str) -> float | None:
    m = _WEIGHT_RE.fullmatch(text.strip())
    if not m:
        return None
    value = float(m.group(2).replace(",", "."))
    return value if 30 <= value <= 300 else None


def parse_steps(text: str) -> int | None:
    m = _STEPS_BEFORE.search(text) or _STEPS_AFTER.search(text)
    if not m:
        return None
    n = int(re.sub(r"\D", "", m.group(1)))
    return n if 500 <= n <= 100_000 else None
