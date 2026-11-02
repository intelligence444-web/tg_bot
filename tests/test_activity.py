"""Активность (v1.1 9): шаги шаблоном, тренировка от модели, половина расхода к норме дня."""
from __future__ import annotations

import json
from datetime import date

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from bot import keyboards, texts
from bot.calc import effective_norm, format_num
from bot.handlers.activity import day_norm_kcal
from tests.fakes import make_callback, make_message, onboard
from tests.test_meals import pasta
from tests.test_text import EGGS, _setup, food_json

TODAY = date(2026, 10, 24)


def run_json(minutes: float = 30, met: float = 8.0, name: str = "бег") -> str:
    return json.dumps({"kind": "activity", "name": name, "minutes": minutes, "met": met}, ensure_ascii=False)


def _delete_data(msg: SendMessage) -> str:
    buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == [texts.BTN_ACTIVITY_DELETE]
    return buttons[0].callback_data


async def _norm(deps) -> float:
    user = await deps.storage.get_user(1)
    return effective_norm(user, TODAY).kcal


async def test_steps_without_model_and_limit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], ai_limit=1)
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    acts = await deps.storage.activities_on(1, TODAY)
    assert len(acts) == 1
    assert acts[0].kcal_burned == 420 and acts[0].kcal_credit == 210
    assert deps.vision.completions == []
    assert await deps.storage.incr_ai_usage(1, TODAY) == 1  # лимит не тронут
    norm = await _norm(deps)
    msg = session.last_message()
    assert msg.text == (
        f"🚶 12 000 шагов — ~420 ккал, к норме сегодня +210 ккал. Осталось: {format_num(norm + 210)} ккал"
    )
    assert msg.parse_mode != "HTML"  # простой текст
    data = _delete_data(msg)
    assert data == f"{keyboards.CB_ACT_DEL}{acts[0].activity_id}"
    assert len(acts[0].activity_id) == 32


async def test_steps_use_trend_weight(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    await dp.feed_update(bot, make_message("80"))  # взвешивание: тренд 80 кг
    await dp.feed_update(bot, make_message("10000 шагов"))
    acts = await deps.storage.activities_on(1, TODAY)
    assert acts[0].kcal_burned == 400 and acts[0].kcal_credit == 200


async def test_training_from_model(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [run_json()], onboarded=False)
    await onboard(dp, bot, weight="87", target="75")
    session.clear()
    await dp.feed_update(bot, make_message("пробежал 30 минут"))
    acts = await deps.storage.activities_on(1, TODAY)
    assert len(acts) == 1
    assert acts[0].kcal_burned == 304.5 and acts[0].kcal_credit == 152.25
    assert acts[0].name == "бег"
    norm = await _norm(deps)
    msg = session.last_message()
    assert msg.text == (
        f"🏃 Бег 30 мин — ~305 ккал, к норме сегодня +152 ккал. Осталось: {format_num(norm + 152.25)} ккал"
    )
    assert msg.parse_mode != "HTML"  # простой текст
    _delete_data(msg)
    assert await deps.storage.recent_chat(1, deps.clock()) == []


async def test_voice_prefix_kept(bot_env):
    from bot.handlers.text import handle_text  # noqa: PLC0415
    from tests.fakes import fsm_context  # noqa: PLC0415

    dp, bot, session, deps = await _setup(bot_env, [])
    msg = make_message("x").message.as_(bot)
    await handle_text(msg, fsm_context(dp), deps, "8 тысяч шагов", prefix="🎙 Понял так: 8 тысяч шагов")
    assert session.last_message().text.startswith("🎙 Понял так: 8 тысяч шагов\n\n🚶 8 000 шагов")


async def test_no_profile_hint_nothing_recorded(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [run_json()], onboarded=False)
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    assert session.last_message().text == texts.PROFILE_HINT
    await dp.feed_update(bot, make_message("пробежал 30 минут"))
    assert session.last_message().text == texts.PROFILE_HINT
    assert await deps.storage.activities_on(1, TODAY) == []


async def test_delete_and_delete_again(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    data = _delete_data(session.last_message())
    session.clear()
    await dp.feed_update(bot, make_callback(data))
    assert await deps.storage.activities_on(1, TODAY) == []
    assert session.of_type(EditMessageText)[-1].text == texts.ACTIVITY_REMOVED
    session.clear()
    await dp.feed_update(bot, make_callback(data))
    assert session.of_type(AnswerCallbackQuery)[-1].text == texts.MEAL_DELETED


async def test_day_norm_includes_credit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    user = await deps.storage.get_user(1)
    base = await day_norm_kcal(deps, user, TODAY)
    assert base == effective_norm(user, TODAY).kcal
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    assert await day_norm_kcal(deps, user, TODAY) == base + 210


async def test_diary_shows_activity_and_norm_with_credit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    norm = await _norm(deps)
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    out = session.last_message().text
    assert f"/ {format_num(norm + 210)} ккал" in out
    assert "🏃 Активность: +210 ккал к норме" in out


async def test_photo_remaining_counts_credit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], results=[pasta()])
    norm = await _norm(deps)
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    left = norm + 210 - meal.analysis.kcal
    assert texts.MEAL_LEFT.format(left=format_num(left)) in session.texts()[-1]


async def test_overeat_from_norm_with_credit(bot_env):
    """Еда чуть выше нормы, но ниже нормы с кредитом — перебора нет; ещё сверх — есть."""
    dp, bot, session, deps = await _setup(bot_env, [])
    norm = await _norm(deps)
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    big = dict(EGGS, items=[dict(EGGS["items"][0], kcal=norm + 100, grams=500)], comment="")
    small = dict(EGGS, items=[dict(EGGS["items"][0], kcal=200, grams=100)], comment="")
    deps.vision.texts = [food_json(big), food_json(small)]
    await dp.feed_update(bot, make_message("съел много"))
    assert texts.OVEREAT not in session.texts()[-1]
    await dp.feed_update(bot, make_message("и ещё"))
    assert texts.OVEREAT in session.texts()[-1]


async def test_context_norm_includes_credit(bot_env):
    from bot.handlers.text import build_day_context  # noqa: PLC0415

    dp, bot, session, deps = await _setup(bot_env, [])
    norm = await _norm(deps)
    await dp.feed_update(bot, make_message("прошёл 12 000 шагов"))
    user = await deps.storage.get_user(1)
    ctx = await build_day_context(deps, user, deps.clock())
    assert ctx.norm_kcal == norm + 210


async def test_long_activity_name_capped(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [run_json(name="очень " * 40 + "долгий бег")])
    await dp.feed_update(bot, make_message("пробежал полчаса"))
    acts = await deps.storage.activities_on(1, TODAY)
    assert len(acts) == 1 and len(acts[0].name) <= 60
    assert acts[0].name == ("очень " * 40)[:60].strip()
    assert "Очень очень" in session.last_message().text
