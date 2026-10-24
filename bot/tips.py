from __future__ import annotations

import random
from dataclasses import dataclass

from bot import texts
from bot.calc import format_num
from bot.models import Analysis, MealType


@dataclass(frozen=True)
class Tip:
    rule: str
    variant: int
    text: str


def _pick(rule: str, last_variants: dict[str, int], rng: random.Random, **fmt) -> Tip:
    variants = texts.TIPS[rule]
    last = last_variants.get(rule)
    choices = [i for i in range(len(variants)) if i != last]
    i = rng.choice(choices)
    return Tip(rule, i, variants[i].format(**fmt) if fmt else variants[i])


def choose_tip(
    analysis: Analysis,
    meal_type: MealType,
    day_kcal_before: float,
    norm_kcal: float,
    overeat_sent_today: bool,
    last_variants: dict[str, int],
    rng: random.Random,
) -> Tip | None:
    kcal = analysis.kcal
    if (
        not overeat_sent_today
        and day_kcal_before <= norm_kcal < day_kcal_before + kcal
    ):
        return Tip("overeat", 0, texts.OVEREAT)
    protein_share = analysis.protein * 4 / kcal if kcal > 0 else 0.0
    if kcal >= 300 and protein_share < 0.15:
        return _pick("low_protein", last_variants, rng)
    drinks = [i for i in analysis.items if i.category == "drink" and i.kcal >= 150]
    if drinks:
        return _pick("drink", last_variants, rng, kcal=format_num(drinks[0].kcal))
    if any(i.category == "sweet" for i in analysis.items):
        return _pick("sweet", last_variants, rng)
    if (
        meal_type in ("lunch", "dinner")
        and kcal >= 400
        and not any(i.category == "vegetable" for i in analysis.items)
    ):
        return _pick("no_veg", last_variants, rng)
    if kcal >= 200 and protein_share >= 0.30:
        return _pick("high_protein", last_variants, rng)
    return None
