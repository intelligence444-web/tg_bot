from datetime import date, datetime, timedelta, timezone

from bot.models import Activity, Analysis, ChatMessage, FoodItem, Meal, Norm, Profile, User

UTC_T = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


def make_meal(meal_id: str, hour: int, day: date = date(2026, 10, 24), user_id: int = 1) -> Meal:
    item = FoodItem("Суп", 300, 150, 8, 5, 20, False, "main")
    return Meal(
        user_id=user_id,
        meal_id=meal_id,
        eaten_at=datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc),
        local_date=day,
        meal_type="lunch",
        analysis=Analysis(True, "high", (item,), False),
    )


def full_user(user_id: int = 1) -> User:
    return User(
        user_id=user_id,
        consent_at=UTC_T,
        profile=Profile("female", 30, 165.0, 70.0, "light", "lose", 60.0),
        norm=Norm(1400.0, 1900.0, 1500.0, 90.0, 50.0, 180.0, 2000.0),
        start_weight_kg=70.0,
        goal_started_at=date(2026, 10, 1),
        onboarded_at=date(2026, 10, 1),
        maintain_transition_until=date(2026, 11, 1),
        transition_from_kcal=1650.0,
        milestones_sent={"a", "b"},
        course_day=3,
        tips_enabled=False,
        is_active=True,
        last_overeat_date=date(2026, 10, 20),
        last_tip_variants={"water": 2},
        first_seen_at=datetime(2026, 9, 30, 7, 15, 30, 123456, tzinfo=timezone.utc),
        next_topic=4,
        last_topic_offer_date=date(2026, 10, 22),
        last_profile_hint_date=date(2026, 10, 21),
    )


def make_activity(
    activity_id: str, hour: int, day: date = date(2026, 10, 24), user_id: int = 1, name: str = "Ходьба"
) -> Activity:
    return Activity(
        user_id=user_id,
        activity_id=activity_id,
        local_date=day,
        name=name,
        kcal_burned=200.0,
        kcal_credit=100.0,
        created_at=datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc),
    )


async def test_user_roundtrip_all_fields(storage):
    user = full_user()
    await storage.save_user(user)
    assert await storage.get_user(1) == user
    assert await storage.get_user(999) is None


async def test_user_returned_is_copy(storage):
    await storage.save_user(User(user_id=1))
    got = await storage.get_user(1)
    got.course_day = 7
    got.milestones_sent.add("x")
    assert (await storage.get_user(1)).course_day == 0
    assert (await storage.get_user(1)).milestones_sent == set()


async def test_user_saved_is_copy(storage):
    user = User(user_id=1)
    await storage.save_user(user)
    user.course_day = 5
    assert (await storage.get_user(1)).course_day == 0


async def test_meals_on_by_local_date_ordered(storage):
    await storage.add_meal(make_meal("b", 14))
    await storage.add_meal(make_meal("a", 8))
    await storage.add_meal(make_meal("c", 10, day=date(2026, 10, 23)))
    await storage.add_meal(make_meal("d", 9, user_id=2))
    meals = await storage.meals_on(1, date(2026, 10, 24))
    assert [m.meal_id for m in meals] == ["a", "b"]


async def test_get_update_delete_meal(storage):
    meal = make_meal("a", 8)
    await storage.add_meal(meal)
    assert await storage.get_meal(1, "a") == meal
    meal.fat_answer = "10"
    await storage.update_meal(meal)
    assert (await storage.get_meal(1, "a")).fat_answer == "10"
    assert await storage.delete_meal(1, "a") is True
    assert await storage.get_meal(1, "a") is None
    assert await storage.delete_meal(1, "a") is False


async def test_meal_days(storage):
    await storage.add_meal(make_meal("a", 8, day=date(2026, 10, 20)))
    await storage.add_meal(make_meal("b", 8, day=date(2026, 10, 22)))
    await storage.add_meal(make_meal("c", 9, day=date(2026, 10, 22)))
    await storage.add_meal(make_meal("d", 8, day=date(2026, 10, 10)))
    await storage.add_meal(make_meal("e", 8, day=date(2026, 10, 22), user_id=2))
    assert await storage.meal_days(1, date(2026, 10, 15)) == {date(2026, 10, 20), date(2026, 10, 22)}


async def test_upsert_weight_replaces_same_day_sorted(storage):
    await storage.upsert_weight(1, date(2026, 10, 24), 69.0)
    await storage.upsert_weight(1, date(2026, 10, 20), 70.0)
    await storage.upsert_weight(1, date(2026, 10, 24), 68.5)
    ws = await storage.weights(1)
    assert [(w.day, w.weight_kg) for w in ws] == [(date(2026, 10, 20), 70.0), (date(2026, 10, 24), 68.5)]
    assert await storage.weights(2) == []


async def test_mark_processed(storage):
    assert await storage.mark_processed("update:1", UTC_T) is True
    assert await storage.mark_processed("update:1", UTC_T) is False
    assert await storage.mark_processed("update:2", UTC_T) is True


async def test_alert_allowed(storage):
    iv = timedelta(hours=1)
    assert await storage.alert_allowed("vision", UTC_T, iv) is True
    assert await storage.alert_allowed("vision", UTC_T + timedelta(minutes=30), iv) is False
    assert await storage.alert_allowed("vision", UTC_T + timedelta(minutes=61), iv) is True
    assert await storage.alert_allowed("other", UTC_T, iv) is True


async def test_daily_users_filter(storage):
    day = date(2026, 10, 1)
    await storage.save_user(User(user_id=1, onboarded_at=day))
    await storage.save_user(User(user_id=2, onboarded_at=day, is_active=False))
    await storage.save_user(User(user_id=3, onboarded_at=day, tips_enabled=False))
    await storage.save_user(User(user_id=4))
    assert [u.user_id for u in await storage.daily_users()] == [1]


async def test_user_new_fields_defaults(storage):
    await storage.save_user(User(user_id=1))
    got = await storage.get_user(1)
    assert got.first_seen_at is None and got.next_topic == 0
    assert got.last_topic_offer_date is None and got.last_profile_hint_date is None


async def test_activities_on_and_get(storage):
    await storage.add_activity(make_activity("b", 14))
    await storage.add_activity(make_activity("a", 8, name="Бег"))
    await storage.add_activity(make_activity("c", 10, day=date(2026, 10, 23)))
    await storage.add_activity(make_activity("d", 9, user_id=2))
    acts = await storage.activities_on(1, date(2026, 10, 24))
    assert [a.activity_id for a in acts] == ["a", "b"]
    assert acts[0] == make_activity("a", 8, name="Бег")
    assert await storage.get_activity(1, "c") == make_activity("c", 10, day=date(2026, 10, 23))
    assert await storage.get_activity(1, "zzz") is None
    assert await storage.get_activity(2, "a") is None


async def test_activities_between_inclusive(storage):
    for i, d in enumerate((19, 20, 22, 24, 25)):
        await storage.add_activity(make_activity(f"x{i}", 8, day=date(2026, 10, d)))
    await storage.add_activity(make_activity("o", 8, day=date(2026, 10, 22), user_id=2))
    acts = await storage.activities_between(1, date(2026, 10, 20), date(2026, 10, 24))
    assert [a.activity_id for a in acts] == ["x1", "x2", "x3"]


async def test_delete_activity(storage):
    await storage.add_activity(make_activity("a", 8))
    assert await storage.delete_activity(1, "a") is True
    assert await storage.get_activity(1, "a") is None
    assert await storage.delete_activity(1, "a") is False
    assert await storage.activities_on(1, date(2026, 10, 24)) == []


def _msg(minutes: int, text: str = "", role: str = "user") -> ChatMessage:
    return ChatMessage(role, text or f"m{minutes}", UTC_T + timedelta(minutes=minutes))


async def test_recent_chat_order_and_limit(storage):
    for m in (5, 1, 3, 12, 2, 4, 6, 7, 8, 9, 10, 11):
        await storage.add_chat_message(1, _msg(m, role="assistant" if m % 2 else "user"))
    await storage.add_chat_message(2, _msg(13))
    now = UTC_T + timedelta(hours=1)
    got = await storage.recent_chat(1, now)
    assert [c.text for c in got] == [f"m{m}" for m in range(3, 13)]
    assert got[0] == ChatMessage("assistant", "m3", UTC_T + timedelta(minutes=3))
    assert [c.text for c in await storage.recent_chat(1, now, limit=2)] == ["m11", "m12"]
    assert await storage.recent_chat(3, now) == []


async def test_recent_chat_skips_older_than_7_days(storage):
    await storage.add_chat_message(1, _msg(0, "old"))
    await storage.add_chat_message(1, _msg(60 * 24 * 7 - 1, "fresh"))
    now = UTC_T + timedelta(days=7, minutes=30)
    assert [c.text for c in await storage.recent_chat(1, now)] == ["fresh"]


async def test_chat_text_truncated_to_2000(storage):
    await storage.add_chat_message(1, ChatMessage("assistant", "я" * 2001, UTC_T))
    (got,) = await storage.recent_chat(1, UTC_T + timedelta(minutes=1))
    assert got.text == "я" * 2000


async def test_chat_same_timestamp_both_kept_in_order(storage):
    await storage.add_chat_message(1, ChatMessage("user", "вопрос", UTC_T))
    await storage.add_chat_message(1, ChatMessage("assistant", "ответ", UTC_T))
    await storage.add_chat_message(1, ChatMessage("user", "ещё", UTC_T))
    got = await storage.recent_chat(1, UTC_T + timedelta(minutes=1))
    assert [c.text for c in got] == ["вопрос", "ответ", "ещё"]
    assert len({c.created_at for c in got}) == 3
    assert all(timedelta(0) <= c.created_at - UTC_T < timedelta(milliseconds=1) for c in got)


async def test_incr_ai_usage(storage):
    d1, d2 = date(2026, 10, 24), date(2026, 10, 25)
    assert await storage.incr_ai_usage(1, d1) == 1
    assert await storage.incr_ai_usage(1, d1) == 2
    assert await storage.incr_ai_usage(1, d2) == 1
    assert await storage.incr_ai_usage(2, d1) == 1
    assert await storage.incr_ai_usage(1, d1) == 3


async def test_meals_between_inclusive_ordered(storage):
    await storage.add_meal(make_meal("a", 8, day=date(2026, 10, 19)))
    await storage.add_meal(make_meal("b", 14, day=date(2026, 10, 20)))
    await storage.add_meal(make_meal("c", 8, day=date(2026, 10, 20)))
    await storage.add_meal(make_meal("d", 8, day=date(2026, 10, 26)))
    await storage.add_meal(make_meal("e", 9, day=date(2026, 10, 27)))
    await storage.add_meal(make_meal("f", 9, day=date(2026, 10, 22), user_id=2))
    meals = await storage.meals_between(1, date(2026, 10, 20), date(2026, 10, 26))
    assert [m.meal_id for m in meals] == ["c", "b", "d"]
