from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from typing import Protocol

from bot.models import Meal, User, Weighing

_PROCESSED_TTL = timedelta(days=1)


class Storage(Protocol):
    async def get_user(self, user_id: int) -> User | None: ...

    async def save_user(self, user: User) -> None: ...

    async def add_meal(self, meal: Meal) -> None: ...

    async def update_meal(self, meal: Meal) -> None: ...

    async def get_meal(self, user_id: int, meal_id: str) -> Meal | None: ...

    async def delete_meal(self, user_id: int, meal_id: str) -> bool: ...

    async def meals_on(self, user_id: int, day: date) -> list[Meal]: ...

    async def meal_days(self, user_id: int, since: date) -> set[date]: ...

    async def upsert_weight(self, user_id: int, day: date, weight_kg: float) -> None: ...

    async def weights(self, user_id: int) -> list[Weighing]: ...

    async def mark_processed(self, key: str, now: datetime) -> bool: ...

    async def alert_allowed(self, kind: str, now: datetime, interval: timedelta) -> bool: ...

    async def daily_users(self) -> list[User]: ...


class MemoryStorage(Storage):
    """Хранилище в памяти; отдаёт и принимает глубокие копии."""

    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._meals: dict[tuple[int, str], Meal] = {}
        self._weights: dict[int, dict[date, float]] = {}
        self._processed: dict[str, datetime] = {}
        self._alerts: dict[str, datetime] = {}

    async def get_user(self, user_id: int) -> User | None:
        return copy.deepcopy(self._users.get(user_id))

    async def save_user(self, user: User) -> None:
        self._users[user.user_id] = copy.deepcopy(user)

    async def add_meal(self, meal: Meal) -> None:
        self._meals[(meal.user_id, meal.meal_id)] = copy.deepcopy(meal)

    async def update_meal(self, meal: Meal) -> None:
        self._meals[(meal.user_id, meal.meal_id)] = copy.deepcopy(meal)

    async def get_meal(self, user_id: int, meal_id: str) -> Meal | None:
        return copy.deepcopy(self._meals.get((user_id, meal_id)))

    async def delete_meal(self, user_id: int, meal_id: str) -> bool:
        return self._meals.pop((user_id, meal_id), None) is not None

    async def meals_on(self, user_id: int, day: date) -> list[Meal]:
        found = [m for m in self._meals.values() if m.user_id == user_id and m.local_date == day]
        found.sort(key=lambda m: m.eaten_at)
        return copy.deepcopy(found)

    async def meal_days(self, user_id: int, since: date) -> set[date]:
        return {m.local_date for m in self._meals.values() if m.user_id == user_id and m.local_date >= since}

    async def upsert_weight(self, user_id: int, day: date, weight_kg: float) -> None:
        self._weights.setdefault(user_id, {})[day] = weight_kg

    async def weights(self, user_id: int) -> list[Weighing]:
        return [Weighing(d, w) for d, w in sorted(self._weights.get(user_id, {}).items())]

    async def mark_processed(self, key: str, now: datetime) -> bool:
        self._processed = {k: t for k, t in self._processed.items() if now - t < _PROCESSED_TTL}
        if key in self._processed:
            return False
        self._processed[key] = now
        return True

    async def alert_allowed(self, kind: str, now: datetime, interval: timedelta) -> bool:
        last = self._alerts.get(kind)
        if last is not None and now - last <= interval:
            return False
        self._alerts[kind] = now
        return True

    async def daily_users(self) -> list[User]:
        return copy.deepcopy(
            [u for u in self._users.values() if u.is_active and u.tips_enabled and u.onboarded_at is not None]
        )
