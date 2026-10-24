from datetime import date, datetime, timedelta, timezone

from bot.models import Analysis, FoodItem, Meal, Norm, Profile, User

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
        milestones_sent={"a", "b"},
        course_day=3,
        tips_enabled=False,
        is_active=True,
        last_overeat_date=date(2026, 10, 20),
        last_tip_variants={"water": 2},
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
