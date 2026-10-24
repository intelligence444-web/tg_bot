"""Офлайн-тесты чистых частей YdbStorage: строка таблицы <-> модель, параметры запросов."""

from datetime import date, datetime, timezone

import pytest
import ydb
from ydb import convert

from bot.models import Analysis, FoodItem, Meal, User
from bot.ydb_storage import (
    MEALS_COLUMNS,
    USERS_COLUMNS,
    create_tables_queries,
    meal_from_row,
    meal_to_row,
    typed_params,
    user_from_row,
    user_to_row,
)
from tests.test_storage import full_user, make_meal

UTC = timezone.utc


def _as_db(row: dict) -> dict:
    """Имитирует то, что отдаёт YDB: Timestamp — наивный UTC, Json — уже разобранный объект."""
    import json

    out = {}
    for col, value in row.items():
        if isinstance(value, datetime):
            value = value.astimezone(UTC).replace(tzinfo=None)
        elif isinstance(value, str) and col in {"profile", "norm", "milestones_sent", "last_tip_variants", "analysis"}:
            value = json.loads(value)
        out[col] = value
    return out


def test_user_full_roundtrip():
    user = full_user()
    assert user_from_row(user_to_row(user)) == user
    assert user_from_row(_as_db(user_to_row(user))) == user


def test_user_minimal_roundtrip_nones_and_empty():
    user = User(user_id=5)
    row = user_to_row(user)
    assert row["profile"] is None and row["norm"] is None and row["consent_at"] is None
    back = user_from_row(_as_db(row))
    assert back == user
    assert back.milestones_sent == set() and back.last_tip_variants == {}


def test_user_timestamp_normalized_to_aware_utc():
    user = full_user()
    back = user_from_row(_as_db(user_to_row(user)))
    assert back.consent_at.tzinfo is not None
    assert back.consent_at.utcoffset().total_seconds() == 0


def test_user_reads_raw_int_forms():
    # Если нативные типы в ответе выключены: Timestamp — микросекунды, Date — дни от эпохи.
    user = User(user_id=1, consent_at=datetime(2026, 10, 24, 9, 0, tzinfo=UTC), onboarded_at=date(2026, 10, 1))
    row = _as_db(user_to_row(user))
    row["consent_at"] = int((user.consent_at - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1_000_000)
    row["onboarded_at"] = (date(2026, 10, 1) - date(1970, 1, 1)).days
    assert user_from_row(row) == user


def test_meal_roundtrip_with_items():
    item = FoodItem("Котлета", 120, 260, 18, 17, 9, False, "main")
    oil = FoodItem("Масло", 10, 89.9, 0, 9.99, 0, True, "fat", "подсолнечное")
    meal = Meal(
        user_id=7,
        meal_id="m1",
        eaten_at=datetime(2026, 10, 24, 9, 30, tzinfo=UTC),
        local_date=date(2026, 10, 24),
        meal_type="breakfast",
        analysis=Analysis(True, "medium", (item, oil), True),
        fat_answer="10",
    )
    assert meal_from_row(meal_to_row(meal)) == meal
    back = meal_from_row(_as_db(meal_to_row(meal)))
    assert back == meal
    assert isinstance(back.analysis.items, tuple)


def test_meal_roundtrip_contract_meal():
    meal = make_meal("a", 8)
    assert meal_from_row(_as_db(meal_to_row(meal))) == meal


def test_typed_params_accepted_by_sdk():
    user_params = typed_params(USERS_COLUMNS, user_to_row(full_user()), pk={"user_id"})
    assert set(user_params) == {f"${c}" for c in USERS_COLUMNS}
    pb = convert.query_parameters_to_pb(user_params)
    assert set(pb) == set(user_params)
    assert pb["$user_id"].type.type_id == ydb.PrimitiveType.Int64.proto.type_id
    assert pb["$profile"].type.HasField("optional_type")

    empty = typed_params(USERS_COLUMNS, user_to_row(User(user_id=2)), pk={"user_id"})
    pb = convert.query_parameters_to_pb(empty)
    assert pb["$norm"].value.HasField("null_flag_value")

    meal_params = typed_params(MEALS_COLUMNS, meal_to_row(make_meal("a", 8)), pk={"user_id", "meal_id"})
    convert.query_parameters_to_pb(meal_params)


def test_create_tables_queries_shape():
    queries = create_tables_queries("t1_")
    text = "\n".join(queries)
    for name in ("users", "meals", "weights", "fsm", "processed_updates", "alerts"):
        assert f"`t1_{name}`" in text
    assert 'TTL = Interval("P1D") ON created_at' in text
    assert "key Utf8 NOT NULL" in text


def test_bad_prefix_rejected():
    with pytest.raises(ValueError):
        create_tables_queries("x`; DROP TABLE users; --")


def test_user_transition_from_kcal_roundtrip():
    user = full_user()
    row = user_to_row(user)
    assert row["transition_from_kcal"] == 1650.0
    assert "transition_from_kcal" in USERS_COLUMNS
    assert user_from_row(_as_db(row)).transition_from_kcal == 1650.0
    assert user_from_row(_as_db(user_to_row(User(user_id=3)))).transition_from_kcal is None
