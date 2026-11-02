"""Хранилище на YDB (serverless) и хранилище состояний aiogram.

Авторизация (см. ``connect``): при ``YDB_METADATA_CREDENTIALS=1`` (ставится в Cloud Functions)
токен берётся из сервиса метаданных; иначе — ``ydb.credentials_from_env_variables()``
(например, ``YDB_ANONYMOUS_CREDENTIALS=1`` для локальной базы).

Время в базе — UTC (Timestamp); при чтении приводится к datetime с tzinfo=UTC.
Сложные поля (profile, norm, milestones_sent, last_tip_variants, analysis, данные FSM) — колонки Json.
Все запросы параметризованы; в текст запроса подставляется только проверенный префикс таблиц.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

import ydb
import ydb.aio
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from ydb import convert

from bot.models import CHAT_TEXT_MAX, Activity, Analysis, ChatMessage, FoodItem, Meal, Norm, Profile, User, Weighing
from bot.storage import CHAT_WINDOW, free_chat_timestamp

T = ydb.PrimitiveType
UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_DATE = date(1970, 1, 1)
_PROCESSED_TTL = timedelta(days=1)
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]*$")

# Схема таблиц: колонка -> тип. Первичные ключи — в _PK.
USERS_COLUMNS: dict[str, T] = {
    "user_id": T.Int64,
    "consent_at": T.Timestamp,
    "profile": T.Json,
    "norm": T.Json,
    "start_weight_kg": T.Double,
    "goal_started_at": T.Date,
    "onboarded_at": T.Date,
    "maintain_transition_until": T.Date,
    "transition_from_kcal": T.Double,
    "milestones_sent": T.Json,
    "course_day": T.Int32,
    "tips_enabled": T.Bool,
    "is_active": T.Bool,
    "last_overeat_date": T.Date,
    "last_tip_variants": T.Json,
    # v1.1 — для уже созданной базы добавляются ALTER-ом (users_alter_ddl)
    "first_seen_at": T.Timestamp,
    "next_topic": T.Int32,
    "last_topic_offer_date": T.Date,
    "last_profile_hint_date": T.Date,
    "updated_at": T.Timestamp,
}
USERS_V11_COLUMNS = ("first_seen_at", "next_topic", "last_topic_offer_date", "last_profile_hint_date")
MEALS_COLUMNS: dict[str, T] = {
    "user_id": T.Int64,
    "meal_id": T.Utf8,
    "eaten_at": T.Timestamp,
    "local_date": T.Date,
    "meal_type": T.Utf8,
    "analysis": T.Json,
    "kcal": T.Double,
    "fat_answer": T.Utf8,
}
WEIGHTS_COLUMNS: dict[str, T] = {
    "user_id": T.Int64,
    "local_date": T.Date,
    "weight_kg": T.Double,
    "created_at": T.Timestamp,
}
FSM_COLUMNS: dict[str, T] = {"key": T.Utf8, "state": T.Utf8, "data_json": T.Json}
PROCESSED_COLUMNS: dict[str, T] = {"key": T.Utf8, "created_at": T.Timestamp}
ALERTS_COLUMNS: dict[str, T] = {"kind": T.Utf8, "last_sent_at": T.Timestamp}
# Ключ (user_id, created_at): уникальность времени обеспечивает запись (free_chat_timestamp в транзакции).
CHAT_COLUMNS: dict[str, T] = {"user_id": T.Int64, "created_at": T.Timestamp, "role": T.Utf8, "text": T.Utf8}
ACTIVITIES_COLUMNS: dict[str, T] = {
    "user_id": T.Int64,
    "activity_id": T.Utf8,
    "local_date": T.Date,
    "name": T.Utf8,
    "kcal_burned": T.Double,
    "kcal_credit": T.Double,
    "created_at": T.Timestamp,
}
AI_USAGE_COLUMNS: dict[str, T] = {"user_id": T.Int64, "local_date": T.Date, "count": T.Int32}

_TABLES: dict[str, tuple[dict[str, T], tuple[str, ...]]] = {
    "users": (USERS_COLUMNS, ("user_id",)),
    "meals": (MEALS_COLUMNS, ("user_id", "meal_id")),
    "weights": (WEIGHTS_COLUMNS, ("user_id", "local_date")),
    "fsm": (FSM_COLUMNS, ("key",)),
    "processed_updates": (PROCESSED_COLUMNS, ("key",)),
    "alerts": (ALERTS_COLUMNS, ("kind",)),
    "chat_messages": (CHAT_COLUMNS, ("user_id", "created_at")),
    "activities": (ACTIVITIES_COLUMNS, ("user_id", "activity_id")),
    "ai_usage": (AI_USAGE_COLUMNS, ("user_id", "local_date")),
}
_TTL = {
    "processed_updates": 'WITH (TTL = Interval("P1D") ON created_at)',
    "chat_messages": 'WITH (TTL = Interval("P7D") ON created_at)',
}


# ---------- значения: Python <-> YDB ----------


def _check_prefix(prefix: str) -> str:
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f"bad table prefix: {prefix!r}")
    return prefix


def _ts_out(value: datetime | None) -> datetime | None:
    """Для записи: наивное время считаем UTC, aware — переводим в UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _ts_in(value: Any) -> datetime | None:
    """Из YDB: наивный datetime (UTC) или целые микросекунды -> aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return _EPOCH + timedelta(microseconds=int(value))


def _date_in(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _EPOCH_DATE + timedelta(days=int(value))


def _json_out(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _json_in(value: Any) -> Any:
    """Json приходит разобранным (нативный режим) или строкой."""
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def typed_params(columns: Mapping[str, T], row: Mapping[str, Any], pk: set[str]) -> dict[str, tuple[Any, Any]]:
    """Параметры запроса ``{"$col": (value, type)}``; вне первичного ключа тип Optional."""
    params: dict[str, tuple[Any, Any]] = {}
    for col, value in row.items():
        t = columns[col]
        params[f"${col}"] = (value, t if col in pk else ydb.OptionalType(t))
    return params


# ---------- строки таблиц <-> модели ----------


def user_to_row(user: User, now: datetime | None = None) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "consent_at": _ts_out(user.consent_at),
        "profile": _json_out(dataclasses.asdict(user.profile) if user.profile else None),
        "norm": _json_out(dataclasses.asdict(user.norm) if user.norm else None),
        "start_weight_kg": user.start_weight_kg,
        "goal_started_at": user.goal_started_at,
        "onboarded_at": user.onboarded_at,
        "maintain_transition_until": user.maintain_transition_until,
        "transition_from_kcal": user.transition_from_kcal,
        "milestones_sent": _json_out(sorted(user.milestones_sent)),
        "course_day": user.course_day,
        "tips_enabled": user.tips_enabled,
        "is_active": user.is_active,
        "last_overeat_date": user.last_overeat_date,
        "last_tip_variants": _json_out(dict(user.last_tip_variants)),
        "first_seen_at": _ts_out(user.first_seen_at),
        "next_topic": user.next_topic,
        "last_topic_offer_date": user.last_topic_offer_date,
        "last_profile_hint_date": user.last_profile_hint_date,
        "updated_at": _ts_out(now or datetime.now(UTC)),
    }


def user_from_row(row: Mapping[str, Any]) -> User:
    profile = _json_in(row["profile"])
    norm = _json_in(row["norm"])
    milestones = _json_in(row["milestones_sent"])
    variants = _json_in(row["last_tip_variants"])
    course_day = row["course_day"]
    tips_enabled = row["tips_enabled"]
    is_active = row["is_active"]
    next_topic = row.get("next_topic")  # новые колонки v1.1 могут отсутствовать до ALTER
    return User(
        user_id=int(row["user_id"]),
        consent_at=_ts_in(row["consent_at"]),
        profile=Profile(**profile) if profile else None,
        norm=Norm(**norm) if norm else None,
        start_weight_kg=row["start_weight_kg"],
        goal_started_at=_date_in(row["goal_started_at"]),
        onboarded_at=_date_in(row["onboarded_at"]),
        maintain_transition_until=_date_in(row["maintain_transition_until"]),
        transition_from_kcal=row.get("transition_from_kcal"),
        milestones_sent=set(milestones or ()),
        course_day=0 if course_day is None else int(course_day),
        tips_enabled=True if tips_enabled is None else bool(tips_enabled),
        is_active=True if is_active is None else bool(is_active),
        last_overeat_date=_date_in(row["last_overeat_date"]),
        last_tip_variants={str(k): int(v) for k, v in (variants or {}).items()},
        first_seen_at=_ts_in(row.get("first_seen_at")),
        next_topic=0 if next_topic is None else int(next_topic),
        last_topic_offer_date=_date_in(row.get("last_topic_offer_date")),
        last_profile_hint_date=_date_in(row.get("last_profile_hint_date")),
    )


def _analysis_to_json(a: Analysis) -> dict[str, Any]:
    return {
        "is_food": a.is_food,
        "confidence": a.confidence,
        "ask_about_fat": a.ask_about_fat,
        "items": [dataclasses.asdict(i) for i in a.items],
    }


def _analysis_from_json(d: Mapping[str, Any]) -> Analysis:
    return Analysis(
        is_food=d["is_food"],
        confidence=d["confidence"],
        items=tuple(FoodItem(**i) for i in d["items"]),
        ask_about_fat=d["ask_about_fat"],
    )


def meal_to_row(meal: Meal) -> dict[str, Any]:
    return {
        "user_id": meal.user_id,
        "meal_id": meal.meal_id,
        "eaten_at": _ts_out(meal.eaten_at),
        "local_date": meal.local_date,
        "meal_type": meal.meal_type,
        "analysis": _json_out(_analysis_to_json(meal.analysis)),
        "kcal": float(meal.analysis.kcal),
        "fat_answer": meal.fat_answer,
    }


def meal_from_row(row: Mapping[str, Any]) -> Meal:
    return Meal(
        user_id=int(row["user_id"]),
        meal_id=row["meal_id"],
        eaten_at=_ts_in(row["eaten_at"]),
        local_date=_date_in(row["local_date"]),
        meal_type=row["meal_type"],
        analysis=_analysis_from_json(_json_in(row["analysis"])),
        fat_answer=row["fat_answer"],
    )


def activity_to_row(a: Activity) -> dict[str, Any]:
    return {
        "user_id": a.user_id,
        "activity_id": a.activity_id,
        "local_date": a.local_date,
        "name": a.name,
        "kcal_burned": float(a.kcal_burned),
        "kcal_credit": float(a.kcal_credit),
        "created_at": _ts_out(a.created_at),
    }


def activity_from_row(row: Mapping[str, Any]) -> Activity:
    return Activity(
        user_id=int(row["user_id"]),
        activity_id=row["activity_id"],
        local_date=_date_in(row["local_date"]),
        name=row["name"],
        kcal_burned=float(row["kcal_burned"]),
        kcal_credit=float(row["kcal_credit"]),
        created_at=_ts_in(row["created_at"]),
    )


def chat_to_row(user_id: int, msg: ChatMessage) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "created_at": _ts_out(msg.created_at),
        "role": msg.role,
        "text": msg.text[:CHAT_TEXT_MAX],
    }


def chat_from_row(row: Mapping[str, Any]) -> ChatMessage:
    return ChatMessage(role=row["role"], text=row["text"], created_at=_ts_in(row["created_at"]))


# ---------- схема ----------


def table_ddl(prefix: str = "") -> list[str]:
    """DDL всех таблиц (единый источник: create_tables, scripts/create_tables.py --print-yql, docs/deploy.md)."""
    _check_prefix(prefix)
    queries = []
    for name, (columns, pk) in _TABLES.items():
        cols = ",\n    ".join(f"{c} {t}{' NOT NULL' if c in pk else ''}" for c, t in columns.items())
        ttl = f"\n{_TTL[name]}" if name in _TTL else ""
        queries.append(
            f"CREATE TABLE IF NOT EXISTS `{prefix}{name}` (\n    {cols},\n    PRIMARY KEY ({', '.join(pk)})\n){ttl};"
        )
    return queries


create_tables_queries = table_ddl  # прежнее имя


def users_alter_ddl(prefix: str = "") -> list[str]:
    """ALTER для базы, созданной до v1.1: новые колонки ``users`` (типы — из USERS_COLUMNS)."""
    _check_prefix(prefix)
    return [f"ALTER TABLE `{prefix}users` ADD COLUMN {c} {USERS_COLUMNS[c]};" for c in USERS_V11_COLUMNS]


async def create_tables(pool: ydb.aio.QuerySessionPool, prefix: str = "") -> None:
    for query in table_ddl(prefix):
        await pool.execute_with_retries(query)


async def drop_tables(pool: ydb.aio.QuerySessionPool, prefix: str = "") -> None:
    _check_prefix(prefix)
    for name in _TABLES:
        await pool.execute_with_retries(f"DROP TABLE IF EXISTS `{prefix}{name}`;")


# ---------- подключение ----------


def _credentials() -> Any:
    if os.environ.get("YDB_METADATA_CREDENTIALS") == "1":
        # Асинхронный вариант на aiohttp: синхронному ydb.iam.MetadataUrlCredentials нужен requests.
        import ydb.aio.iam  # noqa: PLC0415

        return ydb.aio.iam.MetadataUrlCredentials()
    return ydb.credentials_from_env_variables()


async def connect(endpoint: str, database: str) -> ydb.aio.QuerySessionPool:
    driver = ydb.aio.Driver(endpoint=endpoint, database=database, credentials=_credentials())
    await driver.wait(timeout=10, fail_fast=True)
    return ydb.aio.QuerySessionPool(driver)


async def close(pool: ydb.aio.QuerySessionPool) -> None:
    await pool.stop()
    await pool._driver.stop()  # noqa: SLF001 — у пула нет публичного доступа к драйверу


# ---------- хранилище ----------


def _first_rows(result_sets: list[Any]) -> list[Any]:
    return list(result_sets[0].rows) if result_sets else []


class YdbStorage:
    """Реализация ``bot.storage.Storage`` на YDB; поведение совпадает с MemoryStorage."""

    def __init__(self, pool: ydb.aio.QuerySessionPool, prefix: str = "") -> None:
        self._pool = pool
        p = _check_prefix(prefix)
        self._users = f"`{p}users`"
        self._meals = f"`{p}meals`"
        self._weights = f"`{p}weights`"
        self._processed = f"`{p}processed_updates`"
        self._alerts = f"`{p}alerts`"
        self._chat = f"`{p}chat_messages`"
        self._activities = f"`{p}activities`"
        self._ai_usage = f"`{p}ai_usage`"

    async def _query(self, query: str, params: dict[str, Any] | None = None) -> list[Any]:
        return _first_rows(await self._pool.execute_with_retries(query, params))

    @staticmethod
    def _upsert_sql(table: str, columns: Mapping[str, Any]) -> str:
        cols = ", ".join(columns)
        values = ", ".join(f"${c}" for c in columns)
        return f"UPSERT INTO {table} ({cols}) VALUES ({values});"

    async def get_user(self, user_id: int) -> User | None:
        rows = await self._query(
            f"SELECT * FROM {self._users} WHERE user_id = $user_id;",
            {"$user_id": (user_id, T.Int64)},
        )
        return user_from_row(rows[0]) if rows else None

    async def save_user(self, user: User) -> None:
        row = user_to_row(user)
        await self._query(self._upsert_sql(self._users, row), typed_params(USERS_COLUMNS, row, {"user_id"}))

    async def add_meal(self, meal: Meal) -> None:
        row = meal_to_row(meal)
        await self._query(
            self._upsert_sql(self._meals, row), typed_params(MEALS_COLUMNS, row, {"user_id", "meal_id"})
        )

    async def update_meal(self, meal: Meal) -> None:
        await self.add_meal(meal)

    async def get_meal(self, user_id: int, meal_id: str) -> Meal | None:
        rows = await self._query(
            f"SELECT * FROM {self._meals} WHERE user_id = $user_id AND meal_id = $meal_id;",
            {"$user_id": (user_id, T.Int64), "$meal_id": (meal_id, T.Utf8)},
        )
        return meal_from_row(rows[0]) if rows else None

    async def delete_meal(self, user_id: int, meal_id: str) -> bool:
        params = {"$user_id": (user_id, T.Int64), "$meal_id": (meal_id, T.Utf8)}

        async def callee(tx: ydb.aio.QueryTxContext) -> bool:
            found = _first_rows(
                await convert.aggregate_result_sets_by_index_async(
                    await tx.execute(
                        f"SELECT meal_id FROM {self._meals} WHERE user_id = $user_id AND meal_id = $meal_id;",
                        params,
                    )
                )
            )
            if not found:
                return False
            await _drain(
                await tx.execute(f"DELETE FROM {self._meals} WHERE user_id = $user_id AND meal_id = $meal_id;", params)
            )
            return True

        return await self._pool.retry_tx_async(callee)

    async def meals_on(self, user_id: int, day: date) -> list[Meal]:
        rows = await self._query(
            f"SELECT * FROM {self._meals} WHERE user_id = $user_id AND local_date = $day ORDER BY eaten_at;",
            {"$user_id": (user_id, T.Int64), "$day": (day, T.Date)},
        )
        return [meal_from_row(r) for r in rows]

    async def meal_days(self, user_id: int, since: date) -> set[date]:
        rows = await self._query(
            f"SELECT DISTINCT local_date FROM {self._meals} WHERE user_id = $user_id AND local_date >= $since;",
            {"$user_id": (user_id, T.Int64), "$since": (since, T.Date)},
        )
        return {_date_in(r["local_date"]) for r in rows}

    async def upsert_weight(self, user_id: int, day: date, weight_kg: float) -> None:
        row = {"user_id": user_id, "local_date": day, "weight_kg": float(weight_kg), "created_at": datetime.now(UTC)}
        await self._query(
            self._upsert_sql(self._weights, row), typed_params(WEIGHTS_COLUMNS, row, {"user_id", "local_date"})
        )

    async def weights(self, user_id: int) -> list[Weighing]:
        rows = await self._query(
            f"SELECT local_date, weight_kg FROM {self._weights} WHERE user_id = $user_id ORDER BY local_date;",
            {"$user_id": (user_id, T.Int64)},
        )
        return [Weighing(_date_in(r["local_date"]), r["weight_kg"]) for r in rows]

    async def mark_processed(self, key: str, now: datetime) -> bool:
        now = _ts_out(now)
        params = {"$key": (key, T.Utf8)}

        async def callee(tx: ydb.aio.QueryTxContext) -> bool:
            rows = _first_rows(
                await convert.aggregate_result_sets_by_index_async(
                    await tx.execute(f"SELECT created_at FROM {self._processed} WHERE key = $key;", params)
                )
            )
            # TTL удаляет строки не мгновенно, поэтому возраст проверяем сами.
            if rows and rows[0]["created_at"] is not None and now - _ts_in(rows[0]["created_at"]) < _PROCESSED_TTL:
                return False
            await _drain(
                await tx.execute(
                    f"UPSERT INTO {self._processed} (key, created_at) VALUES ($key, $created_at);",
                    {**params, "$created_at": (now, ydb.OptionalType(T.Timestamp))},
                )
            )
            return True

        return await self._pool.retry_tx_async(callee)

    async def alert_allowed(self, kind: str, now: datetime, interval: timedelta) -> bool:
        now = _ts_out(now)
        params = {"$kind": (kind, T.Utf8)}

        async def callee(tx: ydb.aio.QueryTxContext) -> bool:
            rows = _first_rows(
                await convert.aggregate_result_sets_by_index_async(
                    await tx.execute(f"SELECT last_sent_at FROM {self._alerts} WHERE kind = $kind;", params)
                )
            )
            last = _ts_in(rows[0]["last_sent_at"]) if rows else None
            if last is not None and now - last <= interval:
                return False
            await _drain(
                await tx.execute(
                    f"UPSERT INTO {self._alerts} (kind, last_sent_at) VALUES ($kind, $last_sent_at);",
                    {**params, "$last_sent_at": (now, ydb.OptionalType(T.Timestamp))},
                )
            )
            return True

        return await self._pool.retry_tx_async(callee)

    async def daily_users(self) -> list[User]:
        rows = await self._query(
            f"SELECT * FROM {self._users} WHERE is_active AND tips_enabled AND onboarded_at IS NOT NULL "
            "ORDER BY user_id;"
        )
        return [user_from_row(r) for r in rows]

    async def meals_between(self, user_id: int, start: date, end: date) -> list[Meal]:
        rows = await self._query(
            f"SELECT * FROM {self._meals} WHERE user_id = $user_id AND local_date >= $start AND local_date <= $end "
            "ORDER BY eaten_at;",
            {"$user_id": (user_id, T.Int64), "$start": (start, T.Date), "$end": (end, T.Date)},
        )
        return [meal_from_row(r) for r in rows]

    # --- активности ---

    async def add_activity(self, a: Activity) -> None:
        row = activity_to_row(a)
        await self._query(
            self._upsert_sql(self._activities, row),
            typed_params(ACTIVITIES_COLUMNS, row, {"user_id", "activity_id"}),
        )

    async def get_activity(self, user_id: int, activity_id: str) -> Activity | None:
        rows = await self._query(
            f"SELECT * FROM {self._activities} WHERE user_id = $user_id AND activity_id = $activity_id;",
            {"$user_id": (user_id, T.Int64), "$activity_id": (activity_id, T.Utf8)},
        )
        return activity_from_row(rows[0]) if rows else None

    async def delete_activity(self, user_id: int, activity_id: str) -> bool:
        params = {"$user_id": (user_id, T.Int64), "$activity_id": (activity_id, T.Utf8)}
        where = "WHERE user_id = $user_id AND activity_id = $activity_id"

        async def callee(tx: ydb.aio.QueryTxContext) -> bool:
            if not await _tx_rows(tx, f"SELECT activity_id FROM {self._activities} {where};", params):
                return False
            await _drain(await tx.execute(f"DELETE FROM {self._activities} {where};", params))
            return True

        return await self._pool.retry_tx_async(callee)

    async def activities_on(self, user_id: int, day: date) -> list[Activity]:
        return await self.activities_between(user_id, day, day)

    async def activities_between(self, user_id: int, start: date, end: date) -> list[Activity]:
        rows = await self._query(
            f"SELECT * FROM {self._activities} WHERE user_id = $user_id AND local_date >= $start "
            "AND local_date <= $end ORDER BY created_at;",
            {"$user_id": (user_id, T.Int64), "$start": (start, T.Date), "$end": (end, T.Date)},
        )
        return [activity_from_row(r) for r in rows]

    # --- переписка ---

    async def add_chat_message(self, user_id: int, msg: ChatMessage) -> None:
        created_at = _ts_out(msg.created_at)
        key_params = {"$user_id": (user_id, T.Int64), "$created_at": (created_at, T.Timestamp)}

        async def callee(tx: ydb.aio.QueryTxContext) -> None:
            # Чтение и запись в одной serializable-транзакции: занятое время сдвигаем на 1 мкс.
            taken = await _tx_rows(
                tx,
                f"SELECT created_at FROM {self._chat} WHERE user_id = $user_id AND created_at >= $created_at;",
                key_params,
            )
            free = free_chat_timestamp(created_at, (_ts_in(r["created_at"]) for r in taken))
            row = chat_to_row(user_id, dataclasses.replace(msg, created_at=free))
            await _drain(
                await tx.execute(
                    self._upsert_sql(self._chat, row), typed_params(CHAT_COLUMNS, row, {"user_id", "created_at"})
                )
            )

        await self._pool.retry_tx_async(callee)

    async def recent_chat(self, user_id: int, now: datetime, limit: int = 10) -> list[ChatMessage]:
        if limit <= 0:
            return []
        rows = await self._query(
            f"SELECT created_at, role, text FROM {self._chat} WHERE user_id = $user_id AND created_at >= $since "
            "ORDER BY created_at DESC LIMIT $limit;",
            {
                "$user_id": (user_id, T.Int64),
                "$since": (_ts_out(now) - CHAT_WINDOW, T.Timestamp),
                "$limit": (limit, T.Uint64),
            },
        )
        return [chat_from_row(r) for r in reversed(rows)]

    # --- лимит ИИ ---

    async def incr_ai_usage(self, user_id: int, day: date) -> int:
        params = {"$user_id": (user_id, T.Int64), "$local_date": (day, T.Date)}

        async def callee(tx: ydb.aio.QueryTxContext) -> int:
            rows = await _tx_rows(
                tx,
                f"SELECT count FROM {self._ai_usage} WHERE user_id = $user_id AND local_date = $local_date;",
                params,
            )
            current = rows[0]["count"] if rows else None
            value = (0 if current is None else int(current)) + 1
            await _drain(
                await tx.execute(
                    f"UPSERT INTO {self._ai_usage} (user_id, local_date, count) VALUES ($user_id, $local_date, $count);",
                    {**params, "$count": (value, ydb.OptionalType(T.Int32))},
                )
            )
            return value

        return await self._pool.retry_tx_async(callee)


async def _tx_rows(tx: ydb.aio.QueryTxContext, query: str, params: dict[str, Any]) -> list[Any]:
    """Строки первого результата запроса внутри транзакции (поток дочитывается целиком)."""
    return _first_rows(await convert.aggregate_result_sets_by_index_async(await tx.execute(query, params)))


async def _drain(it: Any) -> None:
    """Дочитывает поток ответа (нужно перед следующим запросом в транзакции)."""
    await convert.aggregate_result_sets_by_index_async(it)


# ---------- состояния aiogram ----------


class YdbFsmStorage(BaseStorage):
    """Состояния и данные анкеты aiogram в таблице ``fsm``; ключ — ``bot_id:chat_id:user_id``.

    Пул общий с ``YdbStorage``, поэтому ``close`` его не закрывает (это делает ``close(pool)``).
    Данные хранятся как Json — в них кладём только JSON-совместимые значения.
    """

    def __init__(self, pool: ydb.aio.QuerySessionPool, prefix: str = "") -> None:
        self._pool = pool
        self._table = f"`{_check_prefix(prefix)}fsm`"

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def _row(self, key: StorageKey) -> Any | None:
        rows = _first_rows(
            await self._pool.execute_with_retries(
                f"SELECT state, data_json FROM {self._table} WHERE key = $key;",
                {"$key": (self._key(key), T.Utf8)},
            )
        )
        return rows[0] if rows else None

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._pool.execute_with_retries(
            f"UPSERT INTO {self._table} (key, state) VALUES ($key, $state);",
            {"$key": (self._key(key), T.Utf8), "$state": (value, ydb.OptionalType(T.Utf8))},
        )

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._row(key)
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self._pool.execute_with_retries(
            f"UPSERT INTO {self._table} (key, data_json) VALUES ($key, $data_json);",
            {"$key": (self._key(key), T.Utf8), "$data_json": (_json_out(dict(data)), ydb.OptionalType(T.Json))},
        )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._row(key)
        data = _json_in(row["data_json"]) if row else None
        return dict(data) if data else {}

    async def close(self) -> None:
        return None


# ---------- для тестов против настоящей базы ----------


@asynccontextmanager
async def temp_tables() -> AsyncIterator[tuple[ydb.aio.QuerySessionPool, str]]:
    """Подключается по YDB_ENDPOINT / YDB_DATABASE, создаёт таблицы с уникальным префиксом, потом удаляет."""
    pool = await connect(os.environ["YDB_ENDPOINT"], os.environ.get("YDB_DATABASE", "/local"))
    prefix = f"test_{uuid.uuid4().hex[:12]}_"
    try:
        await create_tables(pool, prefix)
        yield pool, prefix
    finally:
        try:
            await drop_tables(pool, prefix)
        finally:
            await close(pool)


@asynccontextmanager
async def make_test_storage() -> AsyncIterator[YdbStorage]:
    async with temp_tables() as (pool, prefix):
        yield YdbStorage(pool, prefix)
