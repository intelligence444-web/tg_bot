"""Взвешивание, прогресс, график, поздравления и профиль (спец. 6.6–6.9, 8, 9.1–9.4, 13)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from aiogram.methods import SendMessage, SendPhoto

from bot import texts
from bot.calc import calculate_norm
from bot.handlers.weight import format_progress
from bot.models import Analysis, FoodItem, Meal, User, Weighing
from bot.progress import MILESTONE_ORDER
from tests.fakes import get_fsm_state, make_callback, make_message, onboard

START = date(2026, 10, 24)


def _callbacks(msg: SendMessage) -> list[str]:
    markup = msg.reply_markup
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _env(bot_env, days: int = 8, **onboard_kw):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot, **onboard_kw)
    deps.clock.advance(timedelta(days=days))
    session.clear()
    return dp, bot, session, deps


async def _weigh(dp, bot, session, value: str) -> SendMessage:
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_WEIGH))
    await dp.feed_update(bot, make_message(value))
    return session.last_message()


def _today(deps) -> date:
    from bot.diary import to_local

    return to_local(deps.clock()).date()


# --- weighing ---


async def test_weigh_asks_value_with_morning_hint(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await dp.feed_update(bot, make_message(texts.BTN_WEIGH))
    assert "утром, натощак" in session.last_message().text
    assert await get_fsm_state(dp) == "WeightInput:value"


async def test_first_kg(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    before = (await deps.storage.get_user(1)).norm.kcal
    msg = await _weigh(dp, bot, session, "69")
    assert "Пройдено 1 из 8 кг" in msg.text
    assert "🎯 70 → 62 кг" in msg.text
    assert texts.MILESTONES["first_kg"] in msg.text
    assert "За неделю: −1 кг" in msg.text
    assert "chart" in _callbacks(msg)
    user = await deps.storage.get_user(1)
    assert user.milestones_sent == {"first_kg"}
    expected = calculate_norm(replace(user.profile, weight_kg=69.0))
    assert user.norm.kcal == pytest.approx(expected.kcal)
    assert user.norm.kcal < before
    assert await get_fsm_state(dp) is None
    assert await deps.storage.weights(1) == [Weighing(START, 70.0), Weighing(_today(deps), 69.0)]


async def test_same_day_reweigh_replaces(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "69")
    await _weigh(dp, bot, session, "68,8")
    ws = await deps.storage.weights(1)
    assert ws == [Weighing(START, 70.0), Weighing(_today(deps), 68.8)]


async def test_jump_sends_senior_milestone_only(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    msg = await _weigh(dp, bot, session, "66,5")
    assert texts.MILESTONES["q25"] in msg.text
    assert texts.MILESTONES["first_kg"] not in msg.text
    assert texts.MILESTONES["five_percent"] not in msg.text
    assert len(session.of_type(SendMessage)) == 2  # просьба ввести вес + ответ
    user = await deps.storage.get_user(1)
    assert user.milestones_sent == {"first_kg", "five_percent", "q25"}


def test_milestone_texts():
    assert set(texts.MILESTONES) == set(MILESTONE_ORDER)
    assert "здоров" in texts.MILESTONES["five_percent"]


async def test_weekly_up_water_and_salt(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "69")
    deps.clock.advance(timedelta(days=7))
    msg = await _weigh(dp, bot, session, "69,6")
    assert "это вода и соль" in msg.text
    assert "За неделю: +0,6 кг" in msg.text


async def test_three_weeks_away_offers_recalc(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    msg = None
    for value in ("69", "69,2", "69,4", "69,6"):
        msg = await _weigh(dp, bot, session, value)
        deps.clock.advance(timedelta(days=7))
    deps.clock.advance(timedelta(days=-7))
    assert "norm:recalc" in _callbacks(msg)
    assert "это вода и соль" not in msg.text
    # подменяем норму, чтобы пересчёт был виден
    user = await deps.storage.get_user(1)
    user.norm = replace(user.norm, kcal=3000.0)
    await deps.storage.save_user(user)
    session.clear()
    await dp.feed_update(bot, make_callback("norm:recalc"))
    user = await deps.storage.get_user(1)
    expected = calculate_norm(replace(user.profile, weight_kg=69.6))
    assert user.norm.kcal == pytest.approx(expected.kcal)
    assert session.last_message().text


def _meal(day: date) -> Meal:
    item = FoodItem("Гречка", 150, 165, 6, 1.5, 33, False, "side")
    return Meal(1, f"m{day}", datetime(day.year, day.month, day.day, 9, tzinfo=timezone.utc), day, "breakfast",
                Analysis(True, "high", (item,), False))


async def test_goal_reached_summary_and_maintain(bot_env):
    dp, bot, session, deps = await _env(bot_env, days=15)
    await deps.storage.add_meal(_meal(START))
    await deps.storage.add_meal(_meal(START + timedelta(days=3)))
    msg = await _weigh(dp, bot, session, "62")
    assert "Цель достигнута" in msg.text
    assert "8 кг" in msg.text
    assert "2 недели" in msg.text
    assert "дней с записями в дневнике: 2" in msg.text
    assert {"goal_done:maintain", "goal_done:new"} <= set(_callbacks(msg))
    user = await deps.storage.get_user(1)
    old_kcal = user.norm.kcal  # норма после пересчёта по тренду
    today = _today(deps)

    session.clear()
    await dp.feed_update(bot, make_callback("goal_done:maintain"))
    user = await deps.storage.get_user(1)
    assert user.profile.goal == "maintain"
    assert user.profile.target_weight_kg == pytest.approx(62.0)
    assert user.maintain_transition_until == today + timedelta(days=14)
    assert user.transition_from_kcal == pytest.approx(old_kcal)
    tdee = calculate_norm(replace(user.profile, weight_kg=62.0)).tdee
    assert user.norm.kcal == pytest.approx((old_kcal + tdee) / 2)
    assert session.last_message().text

    # внутри переходного периода норма остаётся средней
    deps.clock.advance(timedelta(days=7))
    msg = await _weigh(dp, bot, session, "62")
    user = await deps.storage.get_user(1)
    assert user.norm.kcal == pytest.approx((old_kcal + tdee) / 2)
    assert "Вес держится в коридоре" in msg.text

    deps.clock.advance(timedelta(days=8))  # 15 дней после выбора
    await _weigh(dp, bot, session, "62")
    user = await deps.storage.get_user(1)
    assert user.norm.kcal == pytest.approx(tdee)


async def test_goal_reached_less_than_week(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    user = await deps.storage.get_user(1)
    user.goal_started_at = _today(deps) - timedelta(days=3)
    await deps.storage.save_user(user)
    msg = await _weigh(dp, bot, session, "62")
    assert "меньше недели" in msg.text


async def test_goal_done_new_restarts_goal_steps(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "62")
    session.clear()
    await dp.feed_update(bot, make_callback("goal_done:new"))
    assert texts.ASK_GOAL in session.last_message().text
    await dp.feed_update(bot, make_callback("goal:lose"))
    await dp.feed_update(bot, make_message("58"))
    user = await deps.storage.get_user(1)
    assert user.start_weight_kg == pytest.approx(62.0)
    assert user.milestones_sent == set()
    assert user.profile.target_weight_kg == 58
    assert user.goal_started_at == _today(deps)
    assert len(await deps.storage.weights(1)) == 2  # тренд не записан как замер


async def test_maintain_corridor(bot_env):
    dp, bot, session, deps = await _env(bot_env, goal="maintain")
    msg = await _weigh(dp, bot, session, "71")
    assert "Вес держится в коридоре ±1,5 кг от 70 кг" in msg.text
    assert "Пройдено" not in msg.text
    assert not {"corridor:back", "corridor:keep"} & set(_callbacks(msg))
    assert (await deps.storage.get_user(1)).milestones_sent == set()

    deps.clock.advance(timedelta(days=8))
    msg = await _weigh(dp, bot, session, "72")
    assert "Тренд ушёл на 2 кг от 70 кг" in msg.text
    assert {"corridor:back", "corridor:keep"} <= set(_callbacks(msg))


async def test_corridor_back_sets_goal_to_target(bot_env):
    dp, bot, session, deps = await _env(bot_env, goal="maintain")
    await _weigh(dp, bot, session, "72")
    await dp.feed_update(bot, make_callback("corridor:back"))
    user = await deps.storage.get_user(1)
    assert user.profile.goal == "lose"
    assert user.profile.target_weight_kg == 70
    assert user.start_weight_kg == pytest.approx(72.0)
    assert user.milestones_sent == set()
    assert user.norm.kcal == pytest.approx(calculate_norm(replace(user.profile, weight_kg=72.0)).kcal)


async def test_corridor_keep_sets_target_to_trend(bot_env):
    dp, bot, session, deps = await _env(bot_env, goal="maintain")
    await _weigh(dp, bot, session, "72")
    await dp.feed_update(bot, make_callback("corridor:keep"))
    user = await deps.storage.get_user(1)
    assert user.profile.goal == "maintain"
    assert user.profile.target_weight_kg == pytest.approx(72.0)
    deps.clock.advance(timedelta(days=1))
    msg = await _weigh(dp, bot, session, "72")
    assert "Вес держится в коридоре ±1,5 кг от 72 кг" in msg.text


# --- chart ---


async def test_chart_needs_two_weighings(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    # только взвешивание анкеты
    await dp.feed_update(bot, make_callback("chart"))
    assert session.last_message().text == texts.CHART_LATER
    assert not session.of_type(SendPhoto)


async def test_chart_sends_photo(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "69")
    session.clear()
    await dp.feed_update(bot, make_callback("chart"))
    photos = session.of_type(SendPhoto)
    assert len(photos) == 1


async def test_chart_error_falls_back_to_text(bot_env, monkeypatch):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "69")

    def boom(*a, **kw):
        raise RuntimeError("render failed")

    monkeypatch.setattr("bot.handlers.weight.render_weight_chart", boom)
    session.clear()
    await dp.feed_update(bot, make_callback("chart"))
    assert not session.of_type(SendPhoto)
    text = session.last_message().text
    assert texts.CHART_UNAVAILABLE in text
    assert "Пройдено 1 из 8 кг" in text


# --- before onboarding / validation / menu ---


async def test_weigh_before_onboarding_starts_it(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    await dp.feed_update(bot, make_callback("consent"))
    await (await _ctx(dp)).clear()  # анкета начата, но не пройдена
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_WEIGH))
    sent = session.texts()
    assert texts.NEED_ONBOARDING in sent
    assert texts.ASK_SEX in sent


async def _ctx(dp):
    from tests.fakes import fsm_context

    return fsm_context(dp)


async def test_bad_weight_asks_again(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    msg = await _weigh(dp, bot, session, "7")
    assert "30" in msg.text and "300" in msg.text
    assert await get_fsm_state(dp) == "WeightInput:value"
    assert len(await deps.storage.weights(1)) == 1


async def test_menu_button_while_waiting_clears_state(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await dp.feed_update(bot, make_message(texts.BTN_WEIGH))
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    assert await get_fsm_state(dp) is None
    assert session.last_message().text.startswith("📅 Сегодня")


# --- profile ---


async def test_profile_shows_data_norm_progress(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "69")
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_PROFILE))
    msg = session.last_message()
    user = await deps.storage.get_user(1)
    from bot.calc import format_num

    assert format_num(user.norm.kcal) in msg.text
    assert "165" in msg.text
    assert "Пройдено 1 из 8 кг" in msg.text
    assert {"chart", "profile:goal", "profile:restart"} <= set(_callbacks(msg))


async def test_profile_before_onboarding(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    await dp.feed_update(bot, make_callback("consent"))
    await (await _ctx(dp)).clear()
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_PROFILE))
    assert texts.NEED_ONBOARDING in session.texts()


async def test_profile_goal_reuses_goal_steps(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    await _weigh(dp, bot, session, "69")
    session.clear()
    await dp.feed_update(bot, make_callback("profile:goal"))
    assert texts.ASK_GOAL in session.last_message().text
    await dp.feed_update(bot, make_callback("goal:lose"))
    await dp.feed_update(bot, make_message("60"))
    user = await deps.storage.get_user(1)
    assert user.start_weight_kg == pytest.approx(69.0)
    assert user.milestones_sent == set()
    assert user.profile.target_weight_kg == 60
    assert user.goal_started_at == _today(deps)


async def test_profile_restart_keeps_onboarded_and_course(bot_env):
    dp, bot, session, deps = await _env(bot_env)
    user = await deps.storage.get_user(1)
    user.course_day = 5
    await deps.storage.save_user(user)
    await dp.feed_update(bot, make_callback("profile:restart"))
    assert session.last_message().text == texts.ASK_SEX
    for upd in (
        make_callback("sex:male"),
        make_message("40"),
        make_message("180"),
        make_message("90"),
        make_callback("act:light"),
        make_callback("goal:lose"),
        make_message("80"),
    ):
        await dp.feed_update(bot, upd)
    user = await deps.storage.get_user(1)
    assert user.profile.sex == "male" and user.profile.weight_kg == 90
    assert user.onboarded_at == START
    assert user.course_day == 5


# --- format_progress ---


def _user(**kw) -> User:
    from bot.models import Profile

    profile = Profile("female", 30, 165.0, 70.0, "moderate", "lose", 62.0)
    base = dict(user_id=1, profile=profile, norm=calculate_norm(profile), start_weight_kg=70.0, goal_started_at=START)
    base.update(kw)
    return User(**base)


def test_format_progress_layout():
    today = date(2026, 11, 21)
    ws = [Weighing(START, 70.0), Weighing(date(2026, 11, 21), 66.8)]
    text = format_progress(_user(), ws, today)
    lines = text.split("\n")
    assert lines[0] == "🎯 70 → 62 кг"
    assert lines[1] == "Пройдено 3,2 из 8 кг  ▓▓▓▓░░░░░░ 40%"
    # темп по регрессии: 3,2 кг за 28 дней = 0,8 кг/нед; 4,8 кг / 0,8 = 6 недель
    assert lines[2] == "Темп 0,8 кг/нед — цель примерно 2 января"


def test_format_progress_trend_stands():
    today = date(2026, 11, 21)
    ws = [Weighing(START, 70.0), Weighing(today, 70.5)]
    text = format_progress(_user(), ws, today)
    assert "Пройдено 0 из 8 кг  ░░░░░░░░░░ 0%" in text
    assert "Тренд пока стоит — это нормально в первые недели" in text


def test_format_progress_planned_pace_when_few_data():
    ws = [Weighing(START, 70.0)]
    text = format_progress(_user(), ws, START)
    # плановый темп 0,4 кг/нед: 8 кг = 20 недель = 140 дней
    assert text.split("\n")[2] == "Темп 0,4 кг/нед — цель примерно 13 марта"
