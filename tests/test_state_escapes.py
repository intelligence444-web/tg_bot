"""Выход из анкеты и правки записи кнопками меню, фото и командами; только личные чаты."""
from __future__ import annotations

import io
from datetime import date, datetime, timezone

from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from PIL import Image

from bot import texts
from bot.handlers.meals import MealEdit
from bot.handlers.onboarding import Onboarding
from bot.handlers.weight import WeightInput
from bot.models import Analysis, FoodItem
from tests.fakes import FakeVision, get_fsm_state, make_callback, make_message, onboard

TODAY = date(2026, 10, 24)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


def _food() -> Analysis:
    return Analysis(True, "high", (FoodItem("Гречка", 150, 165, 6.0, 1.5, 33.0, False, "side"),), False)


async def _ready(bot_env, results=()):
    dp, bot, session, deps = bot_env
    deps.vision = FakeVision(list(results))

    async def fetch_photo(_bot, file_id):
        return _jpeg()

    deps.fetch_photo = fetch_photo
    await onboard(dp, bot)
    session.clear()
    return dp, bot, session, deps


# --- I5a: повторная анкета у пользователя с профилем ---


async def test_restart_onboarding_menu_button_leaves_it(bot_env):
    dp, bot, session, deps = await _ready(bot_env)
    await dp.feed_update(bot, make_callback("profile:restart"))
    assert await get_fsm_state(dp) == Onboarding.sex.state
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    assert await get_fsm_state(dp) is None
    assert session.texts()[-1].startswith("📅 Сегодня")


async def test_new_goal_step_photo_leaves_it(bot_env):
    dp, bot, session, deps = await _ready(bot_env, [_food()])
    await dp.feed_update(bot, make_callback("profile:goal"))
    assert await get_fsm_state(dp) == Onboarding.goal.state
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert await get_fsm_state(dp) is None
    assert len(await deps.storage.meals_on(1, TODAY)) == 1


async def test_restart_onboarding_age_step_weigh_button_leaves_it(bot_env):
    dp, bot, session, deps = await _ready(bot_env)
    await dp.feed_update(bot, make_callback("profile:restart"))
    await dp.feed_update(bot, make_callback("sex:male"))
    await dp.feed_update(bot, make_message(texts.BTN_WEIGH))
    assert await get_fsm_state(dp) == WeightInput.value.state
    assert session.texts()[-1] == texts.ASK_WEIGHING


async def test_first_onboarding_keeps_menu_button_inside(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    await dp.feed_update(bot, make_callback("consent"))
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    assert await get_fsm_state(dp) == Onboarding.sex.state
    assert session.texts()[-1].startswith(texts.USE_BUTTONS)


# --- I5b: правка записи ---


async def _in_edit(bot_env):
    dp, bot, session, deps = await _ready(bot_env, [_food()])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"meal:edit:{meal.meal_id}"))
    assert await get_fsm_state(dp) == MealEdit.correction.state
    session.clear()
    return dp, bot, session, deps


async def test_meal_edit_menu_button_clears_state(bot_env):
    dp, bot, session, deps = await _in_edit(bot_env)
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    assert await get_fsm_state(dp) is None
    assert session.texts()[-1].startswith("📅 Сегодня")
    assert [c[0] for c in deps.vision.calls] == ["analyze"]


async def test_meal_edit_command_clears_state(bot_env):
    dp, bot, session, deps = await _in_edit(bot_env)
    await dp.feed_update(bot, make_message("/stop_tips"))
    assert await get_fsm_state(dp) is None
    assert session.texts()[-1] == texts.TIPS_STOPPED


async def test_meal_sauce_menu_button_clears_state(bot_env):
    dp, bot, session, deps = await _ready(bot_env, [Analysis(True, "high", _food().items, True)])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"fat:{meal.meal_id}:sauce"))
    assert await get_fsm_state(dp) == MealEdit.sauce.state
    await dp.feed_update(bot, make_message(texts.BTN_PROFILE))
    assert await get_fsm_state(dp) is None


# --- I5c: старая кнопка «Согласен» ---


async def test_stale_consent_tap_keeps_questionnaire(bot_env):
    dp, bot, session, deps = bot_env
    for upd in (make_message("/start"), make_callback("consent"), make_callback("sex:female"), make_message("30")):
        await dp.feed_update(bot, upd)
    assert await get_fsm_state(dp) == Onboarding.height.state
    session.clear()
    await dp.feed_update(bot, make_callback("consent"))
    assert await get_fsm_state(dp) == Onboarding.height.state
    assert session.texts() == []


# --- M7: только личные чаты ---


def _group_message(text: str) -> Update:
    msg = Message(
        message_id=1,
        date=datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc),
        chat=Chat(id=-100500, type="supergroup", title="Группа"),
        from_user=TgUser(id=1, is_bot=False, first_name="Тест"),
        text=text,
    )
    return Update(update_id=55501, message=msg)


async def test_group_start_is_ignored(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, _group_message("/start"))
    assert session.requests == []
    assert await deps.storage.get_user(1) is None
