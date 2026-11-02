from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from bot.models import CHAT_TEXT_MAX, Activity, ChatMessage, Meal, User, Weighing

_PROCESSED_TTL = timedelta(days=1)
CHAT_WINDOW = timedelta(days=7)  # столько живёт переписка (TTL таблицы chat_messages)
_US = timedelta(microseconds=1)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def free_chat_timestamp(created_at: datetime, taken: Iterable[datetime]) -> datetime:
    """Первое свободное время реплики, начиная с ``created_at``, с шагом 1 мкс (aware UTC).

    Ключ переписки — (user_id, created_at); две реплики в одну микросекунду иначе затёрли бы
    друг друга. ``taken`` — уже занятые времена этого пользователя (наивные считаются UTC).
    """
    busy = {_utc(t) for t in taken}
    candidate = _utc(created_at)
    while candidate in busy:
        candidate += _US
    return candidate


def prepare_chat_message(msg: ChatMessage, taken: Iterable[datetime]) -> ChatMessage:
    """Реплика к записи: текст обрезан до ``CHAT_TEXT_MAX``, время — свободное."""
    return dataclasses.replace(
        msg, text=msg.text[:CHAT_TEXT_MAX], created_at=free_chat_timestamp(msg.created_at, taken)
    )


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

    async def meals_between(self, user_id: int, start: date, end: date) -> list[Meal]:
        """Еда за дни ``start``..``end`` включительно, по времени."""
        ...

    async def add_activity(self, a: Activity) -> None: ...

    async def get_activity(self, user_id: int, activity_id: str) -> Activity | None: ...

    async def delete_activity(self, user_id: int, activity_id: str) -> bool: ...

    async def activities_on(self, user_id: int, day: date) -> list[Activity]: ...

    async def activities_between(self, user_id: int, start: date, end: date) -> list[Activity]:
        """Активности за дни ``start``..``end`` включительно, по ``created_at``."""
        ...

    async def add_chat_message(self, user_id: int, msg: ChatMessage) -> None:
        """Сохраняет реплику; текст обрезается до 2000 знаков, совпавшее время сдвигается на 1 мкс."""
        ...

    async def recent_chat(self, user_id: int, now: datetime, limit: int = 10) -> list[ChatMessage]:
        """Последние ``limit`` реплик за 7 дней до ``now``, по возрастанию времени."""
        ...

    async def incr_ai_usage(self, user_id: int, day: date) -> int:
        """Атомарно +1 к счётчику запросов к ИИ за день; возвращает новое значение."""
        ...


class MemoryStorage(Storage):
    """Хранилище в памяти; отдаёт и принимает глубокие копии."""

    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._meals: dict[tuple[int, str], Meal] = {}
        self._weights: dict[int, dict[date, float]] = {}
        self._processed: dict[str, datetime] = {}
        self._alerts: dict[str, datetime] = {}
        self._activities: dict[tuple[int, str], Activity] = {}
        self._chat: dict[int, list[ChatMessage]] = {}
        self._ai_usage: dict[tuple[int, date], int] = {}

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

    async def meals_between(self, user_id: int, start: date, end: date) -> list[Meal]:
        found = [m for m in self._meals.values() if m.user_id == user_id and start <= m.local_date <= end]
        found.sort(key=lambda m: m.eaten_at)
        return copy.deepcopy(found)

    async def add_activity(self, a: Activity) -> None:
        self._activities[(a.user_id, a.activity_id)] = a

    async def get_activity(self, user_id: int, activity_id: str) -> Activity | None:
        return self._activities.get((user_id, activity_id))

    async def delete_activity(self, user_id: int, activity_id: str) -> bool:
        return self._activities.pop((user_id, activity_id), None) is not None

    async def activities_on(self, user_id: int, day: date) -> list[Activity]:
        return await self.activities_between(user_id, day, day)

    async def activities_between(self, user_id: int, start: date, end: date) -> list[Activity]:
        found = [a for a in self._activities.values() if a.user_id == user_id and start <= a.local_date <= end]
        found.sort(key=lambda a: a.created_at)
        return found

    async def add_chat_message(self, user_id: int, msg: ChatMessage) -> None:
        history = self._chat.setdefault(user_id, [])
        history.append(prepare_chat_message(msg, (m.created_at for m in history)))
        history.sort(key=lambda m: m.created_at)

    async def recent_chat(self, user_id: int, now: datetime, limit: int = 10) -> list[ChatMessage]:
        since = _utc(now) - CHAT_WINDOW
        fresh = [m for m in self._chat.get(user_id, []) if m.created_at >= since]
        return fresh[-limit:] if limit > 0 else []

    async def incr_ai_usage(self, user_id: int, day: date) -> int:
        key = (user_id, day)
        self._ai_usage[key] = self._ai_usage.get(key, 0) + 1
        return self._ai_usage[key]
