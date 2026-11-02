"""Предложение темы (v1.1 8): кнопка «💡 <тема>» под первой едой после 18:00."""
from __future__ import annotations

from datetime import date, datetime, timezone

from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage

from bot import keyboards, texts
from bot.models import ChatMessage
from tests.fakes import FakeVision, make_callback, make_message, onboard
from tests.test_meals import JPEG, pasta
from tests.test_text import chat_json, food_json

TODAY = date(2026, 10, 24)
EVENING = datetime(2026, 10, 24, 15, 30, tzinfo=timezone.utc)  # 18:30 МСК
DAY = datetime(2026, 10, 24, 14, 30, tzinfo=timezone.utc)  # 17:30 МСК


async def _setup(bot_env, *, results=None, replies=None, now=EVENING):
    dp, bot, session, deps = bot_env
    deps.vision = FakeVision(results or [], texts=replies or [])

    async def fetch_photo(_bot, file_id: str) -> bytes:
        return JPEG

    deps.fetch_photo = fetch_photo
    await onboard(dp, bot)
    deps.clock.set(now)
    session.clear()
    return dp, bot, session, deps


def _topic_buttons(markup) -> list[tuple[str, str]]:
    if markup is None:
        return []
    return [
        (b.text, b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data and b.callback_data.startswith(keyboards.CB_TOPIC)
    ]


def _with_markup(upd, markup):
    msg = upd.callback_query.message.model_copy(update={"reply_markup": markup})
    cq = upd.callback_query.model_copy(update={"message": msg})
    return upd.model_copy(update={"callback_query": cq})


def _last_markup(session):
    msgs = [r for r in session.requests if isinstance(r, (SendMessage, EditMessageText))]
    return msgs[-1].reply_markup


async def test_first_evening_meal_offers_topic(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta()])
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == [(f"💡 {texts.TOPICS[0]}", "topic:0")]
    user = await deps.storage.get_user(1)
    assert user.last_topic_offer_date == TODAY
    assert user.next_topic == 0  # сдвигается только по нажатию


async def test_topic_follows_next_topic_modulo(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta()])
    user = await deps.storage.get_user(1)
    user.next_topic = 31
    await deps.storage.save_user(user)
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == [(f"💡 {texts.TOPICS[1]}", "topic:31")]


async def test_text_meal_also_offers_topic(bot_env):
    dp, bot, session, deps = await _setup(bot_env, replies=[food_json()])
    await dp.feed_update(bot, make_message("съел 2 яйца и тост"))
    assert _topic_buttons(_last_markup(session)) == [(f"💡 {texts.TOPICS[0]}", "topic:0")]


async def test_no_topic_before_18(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta()], now=DAY)
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == []
    assert (await deps.storage.get_user(1)).last_topic_offer_date is None


async def test_no_topic_second_time_today(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta(), pasta()])
    await dp.feed_update(bot, make_message(photo=True))
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == []


async def test_no_topic_when_evening_meal_already_recorded(bot_env):
    """Не первая еда после 18:00 (предложение не ставилось, например из-за вопроса) — без кнопки."""
    dp, bot, session, deps = await _setup(bot_env, results=[pasta(), pasta()])
    await dp.feed_update(bot, make_message(photo=True))
    user = await deps.storage.get_user(1)
    user.last_topic_offer_date = None
    await deps.storage.save_user(user)
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == []


async def test_daytime_meal_does_not_block_evening_offer(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta(), pasta()], now=DAY)
    await dp.feed_update(bot, make_message(photo=True))
    deps.clock.set(EVENING)
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == [(f"💡 {texts.TOPICS[0]}", "topic:0")]


async def test_no_topic_when_asked_ai_today(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta()])
    await deps.storage.add_chat_message(1, ChatMessage("user", "вопрос", DAY))
    await deps.storage.add_chat_message(1, ChatMessage("assistant", "ответ", DAY))
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == []
    assert (await deps.storage.get_user(1)).last_topic_offer_date is None


async def test_yesterday_question_does_not_block(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta()])
    yesterday = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)  # 21:00 МСК 23-го
    await deps.storage.add_chat_message(1, ChatMessage("user", "вопрос", yesterday))
    await dp.feed_update(bot, make_message(photo=True))
    assert len(_topic_buttons(_last_markup(session))) == 1


async def test_no_topic_after_stop_tips(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta()])
    await dp.feed_update(bot, make_message("/stop_tips"))
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert _topic_buttons(_last_markup(session)) == []


async def test_topic_button_kept_after_fat_answer(bot_env):
    dp, bot, session, deps = await _setup(bot_env, results=[pasta(ask=True)])
    await dp.feed_update(bot, make_message(photo=True))
    markup = _last_markup(session)
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    session.clear()
    await dp.feed_update(bot, _with_markup(make_callback(f"fat:{meal.meal_id}:little"), markup))
    assert _topic_buttons(_last_markup(session)) == [(f"💡 {texts.TOPICS[0]}", "topic:0")]


async def test_topic_press_asks_model_once_and_advances(bot_env):
    dp, bot, session, deps = await _setup(bot_env, replies=[chat_json("Белок держит сытость дольше.")])
    markup = keyboards.meal_result("m1", ask_fat=False, topic=1)
    await dp.feed_update(bot, _with_markup(make_callback("topic:1"), markup))
    assert len(deps.vision.completions) == 1
    last = deps.vision.completions[0]["messages"][-1]["content"]
    assert f"Расскажи коротко: {texts.TOPICS[1]}" in last
    assert "Белок держит сытость дольше." in session.texts()[-1]
    user = await deps.storage.get_user(1)
    assert user.next_topic == 2
    assert await deps.storage.incr_ai_usage(1, TODAY) == 2  # одна единица на нажатие (+1 — эта проверка)
    history = await deps.storage.recent_chat(1, deps.clock())
    assert [(m.role, m.text) for m in history] == [
        ("user", f"Расскажи коротко: {texts.TOPICS[1]}"),
        ("assistant", "Белок держит сытость дольше."),
    ]
    # кнопка темы убрана из сообщения, остальные на месте
    edits = session.of_type(EditMessageReplyMarkup)
    assert len(edits) == 1
    assert _topic_buttons(edits[0].reply_markup) == []
    assert edits[0].reply_markup.inline_keyboard[0][0].callback_data == "meal:edit:m1"


async def test_topic_press_does_not_move_next_topic_back(bot_env):
    dp, bot, session, deps = await _setup(bot_env, replies=[chat_json("Ответ")])
    user = await deps.storage.get_user(1)
    user.next_topic = 5
    await deps.storage.save_user(user)
    await dp.feed_update(bot, make_callback("topic:1"))
    assert (await deps.storage.get_user(1)).next_topic == 5


async def test_topic_press_respects_ai_limit(bot_env):
    dp, bot, session, deps = await _setup(bot_env, replies=[])
    deps.ai_limit = 0
    await dp.feed_update(bot, make_callback("topic:0"))
    assert deps.vision.completions == []
    assert texts.AI_LIMIT in session.texts()


async def test_topic_press_bad_data_ignored(bot_env):
    dp, bot, session, deps = await _setup(bot_env, replies=[])
    await dp.feed_update(bot, make_callback("topic:x"))
    assert deps.vision.completions == []
    assert session.texts() == []


async def test_topic_press_food_reply_is_not_recorded(bot_env):
    """Тема — это вопрос: даже если модель ответила «food», еда не записывается."""
    dp, bot, session, deps = await _setup(bot_env, replies=[food_json()])
    await dp.feed_update(bot, make_callback("topic:0"))
    assert await deps.storage.meals_on(1, TODAY) == []
    assert texts.NOT_UNDERSTOOD in session.texts()
