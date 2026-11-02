"""Маршрутизатор текста (v1.1 6, 7, 13): шаблоны, лимит ИИ, еда, вопросы, рацион."""
from __future__ import annotations

import json
from datetime import date

from aiogram.methods import EditMessageText, SendMessage

from bot import texts
from bot.handlers.meals import MealEdit
from bot.models import Analysis
from bot.vision import VisionUnavailable
from tests.fakes import FakeVision, fsm_context, get_fsm_state, make_callback, make_message, onboard
from tests.test_meals import JPEG, pasta

TODAY = date(2026, 10, 24)

EGGS = {
    "is_food": True,
    "confidence": "medium",
    "items": [
        {"name": "Яйцо варёное", "grams": 100, "kcal": 155, "protein": 12.6, "fat": 10.6, "carbs": 1.1,
         "category": "main", "hidden": False},
        {"name": "Тост", "grams": 30, "kcal": 80, "protein": 2.7, "fat": 1.0, "carbs": 15.0,
         "category": "side", "hidden": False},
    ],
    "ask_about_fat": False,
    "comment": "Хороший белковый старт дня",
}


def food_json(analysis: dict = EGGS) -> str:
    return json.dumps({"kind": "food", "analysis": analysis}, ensure_ascii=False)


def chat_json(reply: str, kind: str = "chat") -> str:
    return json.dumps({"kind": kind, "reply": reply}, ensure_ascii=False)


def plan_json(norm_kcal: float, norm_protein: float, scale: float = 1.0) -> str:
    """Рацион из 4 приёмов; scale=1 — сумма ровно в норму, БЖУ сходятся с ккал."""
    meals = []
    for t in ("breakfast", "lunch", "snack", "dinner"):
        k = norm_kcal / 4 * scale
        p = norm_protein / 4
        f = k * 0.25 / 9
        c = (k - 4 * p - 9 * f) / 4
        meals.append({"type": t, "options": [
            {"text": f"Блюдо {t}", "kcal": round(k, 1), "protein": round(p, 1),
             "fat": round(f, 1), "carbs": round(c, 1)},
        ]})
    return json.dumps({"kind": "plan", "meals": meals, "notes": "Белок по всем приёмам"}, ensure_ascii=False)


async def _setup(bot_env, replies, *, onboarded=True, results=None, ai_limit=50):
    dp, bot, session, deps = bot_env
    deps.vision = FakeVision(results or [], texts=replies)
    deps.ai_limit = ai_limit

    async def fetch_photo(_bot, file_id: str) -> bytes:
        return JPEG

    deps.fetch_photo = fetch_photo
    if onboarded:
        await onboard(dp, bot)
    else:
        await dp.feed_update(bot, make_message("/start"))
        await dp.feed_update(bot, make_callback("consent"))
    session.clear()
    return dp, bot, session, deps


async def test_food_text_is_recorded_like_photo(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [food_json()])
    await dp.feed_update(bot, make_message("съел 2 яйца и тост"))
    meals = await deps.storage.meals_on(1, TODAY)
    assert len(meals) == 1 and meals[0].analysis.kcal == 235
    out = session.texts()[-1]
    assert "🍽 Яйцо варёное — 100 г — 155 ккал" in out
    assert "Осталось" in out
    assert "Хороший белковый старт дня" in out
    markup = session.last_message().reply_markup
    assert markup is not None and any("meal:del:" in b.callback_data for row in markup.inline_keyboard for b in row)
    # еда в переписку не пишется
    assert await deps.storage.recent_chat(1, deps.clock()) == []


async def test_food_text_not_food(bot_env):
    bad = dict(EGGS, is_food=False, items=[])
    dp, bot, session, deps = await _setup(bot_env, [food_json(bad)])
    await dp.feed_update(bot, make_message("съел воздух"))
    assert session.texts()[-1] == texts.NOT_FOOD_TEXT
    assert await deps.storage.meals_on(1, TODAY) == []


async def test_question_gets_chat_answer_and_history(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [chat_json("В банане около **100 ккал**.")])
    await dp.feed_update(bot, make_message("сколько калорий в банане?"))
    msg = session.last_message()
    assert msg.text == "В банане около <b>100 ккал</b>."
    assert msg.parse_mode == "HTML"
    history = await deps.storage.recent_chat(1, deps.clock())
    assert [(m.role, m.text) for m in history] == [
        ("user", "сколько калорий в банане?"),
        ("assistant", "В банане около **100 ккал**."),
    ]


async def test_request_has_context_history_and_system(bot_env):
    from bot import assistant

    dp, bot, session, deps = await _setup(bot_env, [chat_json("Привет!"), chat_json("Ещё раз привет")])
    await dp.feed_update(bot, make_message("привет"))
    await dp.feed_update(bot, make_message("как дела?"))
    first, second = deps.vision.completions
    assert first["system"] == assistant.SYSTEM_PROMPT
    assert len(first["messages"]) == 1
    current = first["messages"][0]
    assert current["role"] == "user"
    assert "Справка из бота" in current["content"] and "Осталось" in current["content"]
    assert current["content"].endswith("привет")
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert second["messages"][0]["content"] == "привет"
    assert second["messages"][1]["content"] == "Привет!"


async def test_chat_client_preferred_over_vision(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    deps.chat = FakeVision([], texts=[chat_json("Ответ")])
    await dp.feed_update(bot, make_message("вопрос"))
    assert len(deps.chat.completions) == 1 and deps.vision.completions == []
    assert session.last_message().text == "Ответ"


async def test_unclear_reply_saved_to_history(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [chat_json("Сколько минут ты занимался?", "unclear")])
    await dp.feed_update(bot, make_message("сходил в зал"))
    assert session.last_message().text == "Сколько минут ты занимался?"
    assert len(await deps.storage.recent_chat(1, deps.clock())) == 2


async def test_plan_ok_without_approx_mark(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    user = await deps.storage.get_user(1)
    deps.vision.texts.append(plan_json(user.norm.kcal, user.norm.protein_g))
    await dp.feed_update(bot, make_message("составь меню на завтра"))
    assert len(deps.vision.completions) == 1
    msg = session.last_message()
    assert msg.parse_mode == "HTML"
    assert texts.PLAN_TITLE in msg.text
    assert "Суммы примерные" not in msg.text
    history = await deps.storage.recent_chat(1, deps.clock())
    assert [m.role for m in history] == ["user", "assistant"]
    assert texts.PLAN_TITLE in history[1].text and "<b>" not in history[1].text


async def test_plan_retry_then_approx_mark(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    user = await deps.storage.get_user(1)
    bad = plan_json(user.norm.kcal, user.norm.protein_g, scale=0.8)
    deps.vision.texts.extend([bad, bad])
    await dp.feed_update(bot, make_message("составь меню"))
    assert len(deps.vision.completions) == 2
    retry = deps.vision.completions[1]["messages"]
    assert retry[-2]["role"] == "assistant"
    assert retry[-1]["role"] == "user" and "Рацион не сошёлся" in retry[-1]["content"]
    assert texts.PLAN_APPROX.format(pct="20") in session.last_message().text


async def test_plan_retry_fixes_it(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    user = await deps.storage.get_user(1)
    deps.vision.texts.extend([
        plan_json(user.norm.kcal, user.norm.protein_g, scale=0.8),
        plan_json(user.norm.kcal, user.norm.protein_g),
    ])
    await dp.feed_update(bot, make_message("составь меню"))
    assert "Суммы примерные" not in session.last_message().text


async def test_plan_without_profile_gives_hint(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [plan_json(2000, 100)], onboarded=False)
    await dp.feed_update(bot, make_message("составь меню"))
    assert session.texts()[-1] == texts.PROFILE_HINT
    assert len(deps.vision.completions) == 1


async def test_long_plan_is_split(bot_env):
    from bot.handlers.text import split_html

    lines = [f"строка {i} " + "x" * 90 for i in range(100)]
    parts = split_html("\n".join(lines))
    assert len(parts) >= 3
    assert all(len(p) <= 4000 for p in parts)
    assert "\n".join(parts) == "\n".join(lines)


async def test_garbage_twice_not_understood(bot_env):
    dp, bot, session, deps = await _setup(bot_env, ["что-то", "не json"])
    await dp.feed_update(bot, make_message("ыыы"))
    assert len(deps.vision.completions) == 2
    assert session.texts()[-1] == texts.NOT_UNDERSTOOD
    assert await deps.storage.recent_chat(1, deps.clock()) == []


async def test_garbage_then_valid(bot_env):
    dp, bot, session, deps = await _setup(bot_env, ["что-то", chat_json("Ок")])
    await dp.feed_update(bot, make_message("ыыы"))
    assert session.last_message().text == "Ок"


async def test_activity_kind_not_understood_for_now(bot_env):
    reply = json.dumps({"kind": "activity", "name": "бег", "minutes": 30, "met": 8})
    dp, bot, session, deps = await _setup(bot_env, [reply])
    await dp.feed_update(bot, make_message("пробежал 30 минут"))
    assert session.texts()[-1] == texts.NOT_UNDERSTOOD
    assert await deps.storage.recent_chat(1, deps.clock()) == []


async def test_ai_limit_blocks_third_text_but_weight_works(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [chat_json("1"), chat_json("2")], ai_limit=2)
    await dp.feed_update(bot, make_message("вопрос 1"))
    await dp.feed_update(bot, make_message("вопрос 2"))
    await dp.feed_update(bot, make_message("вопрос 3"))
    assert len(deps.vision.completions) == 2
    assert session.texts()[-1] == texts.AI_LIMIT
    await dp.feed_update(bot, make_message("85"))
    assert [w.weight_kg for w in await deps.storage.weights(1) if w.day == TODAY] == [85]


async def test_ai_limit_applies_to_photo(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [chat_json("1")], results=[pasta()], ai_limit=1)
    await dp.feed_update(bot, make_message("вопрос"))
    await dp.feed_update(bot, make_message(photo=True))
    assert deps.vision.calls == []
    assert session.texts()[-1] == texts.AI_LIMIT


async def test_album_spends_one_unit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], results=[pasta(), pasta(), pasta()])
    for _ in range(3):
        await dp.feed_update(bot, make_message(photo=True, media_group_id="g7"))
    assert await deps.storage.incr_ai_usage(1, TODAY) == 2  # одна единица + эта проверка


async def test_model_unavailable_overloaded(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [VisionUnavailable("down", no_credits=True)])
    deps.admin_chat_id = 777
    await dp.feed_update(bot, make_message("вопрос"))
    assert texts.OVERLOADED in session.texts()
    assert any(r.chat_id == 777 for r in session.of_type(SendMessage))


async def test_weight_shortcut(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    await dp.feed_update(bot, make_message("вес 68,5"))
    assert [w.weight_kg for w in await deps.storage.weights(1) if w.day == TODAY] == [68.5]
    assert deps.vision.completions == []
    assert await deps.storage.incr_ai_usage(1, TODAY) == 1  # лимит не тронут


async def test_weight_shortcut_without_profile(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], onboarded=False)
    await dp.feed_update(bot, make_message("85"))
    assert session.texts()[-1] == texts.PROFILE_HINT
    assert await deps.storage.weights(1) == []


async def test_weight_in_meal_edit_goes_to_correction(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], results=[pasta(), pasta()])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"meal:edit:{meal.meal_id}"))
    assert await get_fsm_state(dp) == MealEdit.correction.state
    await dp.feed_update(bot, make_message("85"))
    assert deps.vision.calls[-1] == ("correct", meal.analysis, "85")
    assert [w for w in await deps.storage.weights(1) if w.day == TODAY and w.weight_kg == 85] == []


async def test_profile_line_outside_state(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], onboarded=False)
    await dp.feed_update(bot, make_message("жен, 1996, 165, 70"))
    assert await get_fsm_state(dp) is not None
    assert deps.vision.completions == []


async def test_handle_text_prefix_plain_and_html(bot_env):
    from bot.handlers.text import handle_text

    dp, bot, session, deps = await _setup(bot_env, [food_json(), chat_json("Ответ")])
    state = fsm_context(dp)
    msg = make_message("x").message.as_(bot)
    await handle_text(msg, state, deps, "съел 2 яйца", prefix="🎙 Понял так: съел 2 яйца <и>")
    assert session.texts()[-1].startswith("🎙 Понял так: съел 2 яйца <и>\n")
    await handle_text(msg, state, deps, "вопрос", prefix="🎙 Понял так: a<b")
    last = session.last_message()
    assert last.parse_mode == "HTML" and last.text.startswith("🎙 Понял так: a&lt;b\n")


async def test_food_text_without_profile_has_hint(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [food_json()], onboarded=False)
    await dp.feed_update(bot, make_message("съел 2 яйца"))
    assert texts.PROFILE_HINT in session.texts()[-1]
    assert len(await deps.storage.meals_on(1, TODAY)) == 1


async def test_correction_spends_ai_unit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [], results=[pasta()], ai_limit=1)
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"meal:edit:{meal.meal_id}"))
    await dp.feed_update(bot, make_message("это гречка"))
    assert [c for c in deps.vision.calls if c[0] == "correct"] == []
    assert session.texts()[-1] == texts.AI_LIMIT


async def test_empty_reply_after_formatting_not_understood(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [chat_json("**")])
    await dp.feed_update(bot, make_message("вопрос"))
    assert session.texts()[-1] == texts.NOT_UNDERSTOOD


async def test_photo_comment_shown(bot_env):
    a = pasta()
    a = Analysis(a.is_food, a.confidence, a.items, a.ask_about_fat, comment="Добавь овощей")
    dp, bot, session, deps = await _setup(bot_env, [], results=[a])
    await dp.feed_update(bot, make_message(photo=True))
    edits = session.of_type(EditMessageText)
    assert "Добавь овощей" in edits[-1].text



async def test_unknown_command_still_fallback(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    await dp.feed_update(bot, make_message("/unknown"))
    assert session.texts()[-1] == texts.FALLBACK
    assert deps.vision.completions == []
