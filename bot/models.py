from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Sex = Literal["male", "female"]
Activity = Literal["sedentary", "light", "moderate", "high", "very_high"]
Goal = Literal["lose", "maintain", "gain"]
MealType = Literal["breakfast", "lunch", "snack", "dinner"]
Category = Literal["main", "side", "vegetable", "fruit", "sweet", "drink", "sauce", "fat", "other"]

# КБЖУ на 100 г: (ккал, белки, жиры, углеводы)
_BUTTER_PER_100 = (748.0, 0.5, 82.5, 0.8)
_SUNFLOWER_PER_100 = (899.0, 0.0, 99.9, 0.0)


@dataclass(frozen=True)
class Profile:
    sex: Sex
    age: int
    height_cm: float
    weight_kg: float
    activity: Activity
    goal: Goal
    target_weight_kg: float | None


@dataclass(frozen=True)
class Norm:
    bmr: float
    tdee: float
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    water_ml: float


@dataclass(frozen=True)
class FoodItem:
    name: str
    grams: float
    kcal: float
    protein: float
    fat: float
    carbs: float
    hidden: bool
    category: Category
    fat_type: str | None = None


@dataclass(frozen=True)
class Analysis:
    is_food: bool
    confidence: Literal["high", "medium", "low"]
    items: tuple[FoodItem, ...]
    ask_about_fat: bool

    @property
    def kcal(self) -> float:
        return sum(i.kcal for i in self.items)

    @property
    def protein(self) -> float:
        return sum(i.protein for i in self.items)

    @property
    def fat(self) -> float:
        return sum(i.fat for i in self.items)

    @property
    def carbs(self) -> float:
        return sum(i.carbs for i in self.items)

    def with_fat(self, grams: float) -> Analysis:
        """Заменяет все скрытые жиры одной позицией «Масло» указанного веса."""
        old_hidden = [i for i in self.items if i.hidden and i.category == "fat"]
        kept = tuple(i for i in self.items if not (i.hidden and i.category == "fat"))
        if grams == 0:
            return Analysis(self.is_food, self.confidence, kept, False)
        fat_type = next((i.fat_type for i in old_hidden if i.fat_type), None)
        butter = fat_type is not None and "сливоч" in fat_type.lower()
        kcal, protein, fat, carbs = _BUTTER_PER_100 if butter else _SUNFLOWER_PER_100
        k = grams / 100
        oil = FoodItem(
            name="Масло",
            grams=grams,
            kcal=kcal * k,
            protein=protein * k,
            fat=fat * k,
            carbs=carbs * k,
            hidden=True,
            category="fat",
            fat_type=fat_type if butter else "подсолнечное",
        )
        return Analysis(self.is_food, self.confidence, kept + (oil,), False)


@dataclass
class Meal:
    user_id: int
    meal_id: str
    eaten_at: datetime
    local_date: date
    meal_type: MealType
    analysis: Analysis
    fat_answer: str | None = None


@dataclass(frozen=True)
class Weighing:
    day: date
    weight_kg: float


@dataclass
class User:
    user_id: int
    consent_at: datetime | None = None
    profile: Profile | None = None
    norm: Norm | None = None
    start_weight_kg: float | None = None
    goal_started_at: date | None = None
    onboarded_at: date | None = None
    maintain_transition_until: date | None = None
    milestones_sent: set[str] = field(default_factory=set)
    course_day: int = 0
    tips_enabled: bool = True
    is_active: bool = True
    last_overeat_date: date | None = None
    last_tip_variants: dict[str, int] = field(default_factory=dict)
