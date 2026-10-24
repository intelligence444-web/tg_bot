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

from bot.models import Analysis, FoodItem, Meal, Norm, Profile, User, Weighing

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
    "milestones_sent": T.Json,
    "course_day": T.Int32,
    "tips_enabled": T.Bool,
    "is_active": T.Bool,
    "last_overeat_date": T.Date,
    "last_tip_variants": T.Json,
    "updated_at": T.Timestamp,
}
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

_TABLES: dict[str, tuple[dict[str, T], tuple[str, ...]]] = {
    "users": (USERS_COLUMNS, ("user_id",)),
    "meals": (MEALS_COLUMNS, ("user_id", "meal_id")),
    "weights": (WEIGHTS_COLUMNS, ("user_id", "local_date")),
    "fsm": (FSM_COLUMNS, ("key",)),
    "processed_updates": (PROCESSED_COLUMNS, ("key",)),
    "alerts": (ALERTS_COLUMNS, ("kind",)),
}
_TTL = {"processed_updates": 'WITH (TTL = Interval("P1D") ON created_at)'}


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
        "milestones_sent": _json_out(sorted(user.milestones_sent)),
        "course_day": user.course_day,
        "tips_enabled": user.tips_enabled,
        "is_active": user.is_active,
        "last_overeat_date": user.last_overeat_date,
        "last_tip_variants": _json_out(dict(user.last_tip_variants)),
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
    return User(
        user_id=int(row["user_id"]),
        consent_at=_ts_in(row["consent_at"]),
        profile=Profile(**profile) if profile else None,
        norm=Norm(**norm) if norm else None,
        start_weight_kg=row["start_weight_kg"],
        goal_started_at=_date_in(row["goal_started_at"]),
        onboarded_at=_date_in(row["onboarded_at"]),
        maintain_transition_until=_date_in(row["maintain_transition_until"]),
        milestones_sent=set(milestones or ()),
        course_day=0 if course_day is None else int(course_day),
        tips_enabled=True if tips_enabled is None else bool(tips_enabled),
        is_active=True if is_active is None else bool(is_active),
        last_overeat_date=_date_in(row["last_overeat_date"]),
        last_tip_variants={str(k): int(v) for k, v in (variants or {}).items()},
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


# ---------- схема ----------


def create_tables_queries(prefix: str = "") -> list[str]:
    _check_prefix(prefix)
    queries = []
    for name, (columns, pk) in _TABLES.items():
        cols = ",\n    ".join(f"{c} {t}{' NOT NULL' if c in pk else ''}" for c, t in columns.items())
        ttl = f"\n{_TTL[name]}" if name in _TTL else ""
        queries.append(
            f"CREATE TABLE IF NOT EXISTS `{prefix}{name}` (\n    {cols},\n    PRIMARY KEY ({', '.join(pk)})\n){ttl};"
        )
    return queries


async def create_tables(pool: ydb.aio.QuerySessionPool, prefix: str = "") -> None:
    for query in create_tables_queries(prefix):
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
