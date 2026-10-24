from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from bot.models import Activity, Norm, Profile, Sex

ACTIVITY_FACTORS: dict[Activity, float] = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "high": 1.725,
    "very_high": 1.9,
}

_PROTEIN_PER_KG = {"lose": 1.8, "maintain": 1.4, "gain": 1.6}
_FAT_PER_KG = 0.9
_WATER_ML_PER_KG = 30
_KCAL_PER_KG = 7700


def bmr(sex: Sex, weight_kg: float, height_cm: float, age: int) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "male" else base - 161


def calculate_norm(p: Profile, kcal_override: float | None = None) -> Norm:
    b = bmr(p.sex, p.weight_kg, p.height_cm, p.age)
    tdee = b * ACTIVITY_FACTORS[p.activity]
    if kcal_override is not None:
        kcal = kcal_override
    elif p.goal == "lose":
        kcal = max(tdee * 0.8, b)
    elif p.goal == "gain":
        kcal = tdee * 1.1
    else:
        kcal = tdee
    protein = _PROTEIN_PER_KG[p.goal] * p.weight_kg
    fat = _FAT_PER_KG * p.weight_kg
    carbs = max(0.0, (kcal - protein * 4 - fat * 9) / 4)
    return Norm(
        bmr=b,
        tdee=tdee,
        kcal=kcal,
        protein_g=protein,
        fat_g=fat,
        carbs_g=carbs,
        water_ml=_WATER_ML_PER_KG * p.weight_kg,
    )


def planned_pace_kg_week(norm: Norm) -> float:
    return abs(norm.kcal - norm.tdee) * 7 / _KCAL_PER_KG


def planned_weeks_to_goal(p: Profile, norm: Norm) -> float | None:
    diff = abs(norm.kcal - norm.tdee)
    if p.goal == "maintain" or p.target_weight_kg is None or diff == 0:
        return None
    return abs(p.weight_kg - p.target_weight_kg) * _KCAL_PER_KG / diff / 7


def bmi(weight_kg: float, height_cm: float) -> float:
    return weight_kg / (height_cm / 100) ** 2


def lose_allowed(age: int) -> bool:
    return not 14 <= age <= 15


def target_bmi_low(target_kg: float, height_cm: float) -> bool:
    return bmi(target_kg, height_cm) < 18.5


def round_half_up(x: float, ndigits: int = 0) -> float:
    q = Decimal(1).scaleb(-ndigits)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def format_num(x: float, ndigits: int = 0) -> str:
    r = round_half_up(x, ndigits)
    if r == int(r):
        return f"{int(r):,}".replace(",", " ")
    whole, frac = f"{r:.{ndigits}f}".split(".")
    whole = f"{int(whole):,}".replace(",", " ")
    return f"{whole},{frac}"


def parse_number(text: str, lo: float, hi: float) -> float | None:
    cleaned = "".join(ch for ch in text if not ch.isspace() and not ch.isalpha())
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not lo <= value <= hi:
        return None
    return value
