from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot import texts
from bot.calc import format_num, round_half_up
from bot.models import Meal, MealType, Norm

MSK = ZoneInfo("Europe/Moscow")

_ORDER: tuple[MealType, ...] = ("breakfast", "lunch", "snack", "dinner")


def to_local(dt_utc: datetime) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(MSK)


def meal_type_for(local: datetime) -> MealType:
    minutes = local.hour * 60 + local.minute
    if 5 * 60 <= minutes < 11 * 60:
        return "breakfast"
    if 11 * 60 <= minutes < 16 * 60:
        return "lunch"
    if 17 * 60 <= minutes < 23 * 60:
        return "dinner"
    return "snack"


@dataclass
class DaySummary:
    kcal: float
    protein: float
    fat: float
    carbs: float
    by_type: dict[MealType, float]


def summarize(meals: list[Meal]) -> DaySummary:
    by_type: dict[MealType, float] = {}
    for m in meals:
        by_type[m.meal_type] = by_type.get(m.meal_type, 0.0) + m.analysis.kcal
    return DaySummary(
        kcal=sum(m.analysis.kcal for m in meals),
        protein=sum(m.analysis.protein for m in meals),
        fat=sum(m.analysis.fat for m in meals),
        carbs=sum(m.analysis.carbs for m in meals),
        by_type=by_type,
    )


def format_day(summary: DaySummary, norm: Norm, streak_days: int) -> str:
    filled = max(0, min(10, math.floor(summary.kcal / norm.kcal * 10)))
    bar = "▓" * filled + "░" * (10 - filled)
    pct = format_num(summary.kcal / norm.kcal * 100)
    left = format_num(max(0.0, norm.kcal - summary.kcal))
    lines = [
        texts.DIARY_TITLE.format(
            eaten=format_num(summary.kcal), norm=format_num(norm.kcal), bar=bar, pct=pct
        )
    ]
    if streak_days >= 2:
        word = texts.plural(streak_days, *texts.STREAK_WORDS)
        lines.append(texts.STREAK_LINE.format(n=streak_days, word=word))
    lines.append("")
    for mt in _ORDER:
        label = texts.MEAL_LABELS[mt]
        if mt in summary.by_type:
            lines.append(texts.MEAL_LINE.format(label=label, kcal=format_num(summary.by_type[mt])))
        else:
            lines.append(texts.MEAL_EMPTY.format(label=label, left=left))
    lines.append("")
    lines.append(
        texts.DIARY_PROTEIN.format(
            eaten=format_num(summary.protein), norm=format_num(norm.protein_g)
        )
    )
    lines.append(
        texts.DIARY_FAT_CARBS.format(
            fat=format_num(summary.fat),
            fat_norm=format_num(norm.fat_g),
            carbs=format_num(summary.carbs),
            carbs_norm=format_num(norm.carbs_g),
        )
    )
    return "\n".join(lines)
