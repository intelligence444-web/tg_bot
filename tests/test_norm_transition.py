"""Плавный выход (спец. 9.2): после конца перехода норма — TDEE, даже без нового взвешивания."""
from __future__ import annotations

import io
from dataclasses import replace
from datetime import date, timedelta

from PIL import Image

from bot import texts
from bot.calc import calculate_norm, effective_norm, format_num
from bot.models import Analysis, FoodItem
from tests.fakes import FakeVision, make_message, onboard

TODAY = date(2026, 10, 24)


async def _in_maintain(bot_env, until: date):
    """Пользователь выбрал «Держать вес»: норма — середина между старой и TDEE до ``until``."""
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    user = await deps.storage.get_user(1)
    old_kcal = user.norm.kcal
    profile = replace(user.profile, goal="maintain", target_weight_kg=user.profile.weight_kg)
    tdee = calculate_norm(profile).tdee
    user.profile = profile
    user.norm = calculate_norm(profile, kcal_override=(old_kcal + tdee) / 2)
    user.maintain_transition_until = until
    user.transition_from_kcal = old_kcal
    await deps.storage.save_user(user)
    session.clear()
    return dp, bot, session, deps, user, tdee


def test_effective_norm_unit():
    from bot.models import Profile, User

    p = Profile("female", 30, 165, 70, "moderate", "maintain", 70)
    tdee = calculate_norm(p).tdee
    mid = calculate_norm(p, kcal_override=(1500 + tdee) / 2)
    u = User(1, profile=p, norm=mid, maintain_transition_until=TODAY, transition_from_kcal=1500)
    assert effective_norm(u, TODAY) == mid  # последний день перехода — ещё середина
    assert effective_norm(u, TODAY + timedelta(days=1)).kcal == tdee
    u.maintain_transition_until = None
    assert effective_norm(u, TODAY + timedelta(days=30)) == mid


async def test_today_shows_tdee_after_transition_ended(bot_env):
    dp, bot, session, deps, user, tdee = await _in_maintain(bot_env, TODAY - timedelta(days=3))
    assert round(user.norm.kcal) != round(tdee)
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    assert session.texts()[-1].startswith(f"📅 Сегодня — 0 / {format_num(tdee)} ккал")


async def test_today_shows_mid_norm_during_transition(bot_env):
    dp, bot, session, deps, user, tdee = await _in_maintain(bot_env, TODAY + timedelta(days=3))
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    assert session.texts()[-1].startswith(f"📅 Сегодня — 0 / {format_num(user.norm.kcal)} ккал")


async def test_profile_shows_tdee_after_transition_ended(bot_env):
    dp, bot, session, deps, user, tdee = await _in_maintain(bot_env, TODAY - timedelta(days=3))
    await dp.feed_update(bot, make_message(texts.BTN_PROFILE))
    assert f"{format_num(tdee)} ккал" in session.texts()[-1]
    assert f"{format_num(user.norm.kcal)} ккал" not in session.texts()[-1]


async def test_meal_remaining_uses_tdee_after_transition_ended(bot_env):
    dp, bot, session, deps, user, tdee = await _in_maintain(bot_env, TODAY - timedelta(days=3))
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")

    async def fetch_photo(_bot, file_id):
        return buf.getvalue()

    deps.fetch_photo = fetch_photo
    food = Analysis(True, "high", (FoodItem("Гречка", 150, 165, 6.0, 1.5, 33.0, False, "side"),), False)
    deps.vision = FakeVision([food])
    await dp.feed_update(bot, make_message(photo=True))
    assert texts.MEAL_LEFT.format(left=format_num(tdee - 165)) in session.texts()[-1]
