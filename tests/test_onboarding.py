from datetime import date, timedelta

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from bot import texts
from bot.handlers.onboarding import Onboarding, start_goal_step
from tests.fakes import fsm_context, get_fsm_state, make_callback, make_message, onboard

TODAY = date(2026, 10, 24)


def _callbacks(msg: SendMessage) -> list[str]:
    assert isinstance(msg.reply_markup, InlineKeyboardMarkup)
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row]


async def _feed(dp, bot, *updates):
    for upd in updates:
        await dp.feed_update(bot, upd)


async def _to_step(dp, bot, *, age="30", height="165", weight=None, activity=None, goal=None):
    await _feed(dp, bot, make_message("/start"), make_callback("consent"), make_callback("sex:female"))
    for value in (age, height, weight):
        if value is None:
            return
        await _feed(dp, bot, make_message(value))
    if activity:
        await _feed(dp, bot, make_callback(f"act:{activity}"))
    if goal:
        await _feed(dp, bot, make_callback(f"goal:{goal}"))


async def test_start_shows_welcome_with_consent(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    msg = session.last_message()
    assert "без запретов и без голода" in msg.text
    assert texts.PRIVACY_URL in msg.text
    assert "ориентир, а не медицинские рекомендации" in msg.text
    assert _callbacks(msg) == ["consent"]
    user = await deps.storage.get_user(1)
    assert user is not None and user.consent_at is None


async def test_photo_before_consent_asks_consent_again(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    msg = session.last_message()
    assert msg.text == texts.CONSENT_REQUIRED
    assert _callbacks(msg) == ["consent"]
    assert await get_fsm_state(dp) is None


async def test_consent_starts_questionnaire(bot_env):
    dp, bot, session, deps = bot_env
    await _feed(dp, bot, make_message("/start"), make_callback("consent"))
    user = await deps.storage.get_user(1)
    assert user.consent_at == deps.clock()
    msg = session.last_message()
    assert msg.text == texts.ASK_SEX
    assert _callbacks(msg) == ["sex:male", "sex:female"]
    assert await get_fsm_state(dp) == Onboarding.sex.state


async def test_full_reference_example(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    msg = session.last_message()
    for fragment in ("1 420", "2 201", "1 761", "Белки 126 г", "Жиры 63 г", "Углеводы 173 г",
                     "2,1 л", "0,4 кг", "20 недел", "главное для сытости", "Отправь фото еды"):
        assert fragment in msg.text, fragment
    assert msg.text.index("Белки") < msg.text.index("Жиры") < msg.text.index("Углеводы")
    assert isinstance(msg.reply_markup, ReplyKeyboardMarkup)
    buttons = [b.text for row in msg.reply_markup.keyboard for b in row]
    assert buttons == [texts.BTN_TODAY, texts.BTN_WEIGH, texts.BTN_PROFILE, texts.BTN_HELP]

    user = await deps.storage.get_user(1)
    assert user.profile is not None and user.profile.target_weight_kg == 62
    assert user.profile.goal == "lose" and user.profile.activity == "moderate"
    assert user.norm is not None and round(user.norm.kcal) == 1761
    assert user.start_weight_kg == 70
    assert user.consent_at is not None
    assert user.goal_started_at == TODAY and user.onboarded_at == TODAY
    weights = await deps.storage.weights(1)
    assert [(w.day, w.weight_kg) for w in weights] == [(TODAY, 70)]
    assert await get_fsm_state(dp) is None


async def test_invalid_age(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, age=None)
    await dp.feed_update(bot, make_message("200"))
    text = session.last_message().text
    assert "14" in text and "100" in text
    assert await get_fsm_state(dp) == Onboarding.age.state


async def test_invalid_height(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, height=None)
    await dp.feed_update(bot, make_message("абв"))
    text = session.last_message().text
    assert "120" in text and "230" in text
    assert await get_fsm_state(dp) == Onboarding.height.state


async def test_invalid_weight(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot)
    await dp.feed_update(bot, make_message("20"))
    text = session.last_message().text
    assert "30" in text and "300" in text
    assert await get_fsm_state(dp) == Onboarding.weight.state


async def test_weight_with_comma_and_unit(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, weight="70,5 кг", activity="moderate", goal="maintain")
    user = await deps.storage.get_user(1)
    assert user.profile.weight_kg == 70.5
    assert user.start_weight_kg == 70.5


async def test_text_at_button_step_reasks_with_buttons(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, weight="70")
    session.clear()
    await dp.feed_update(bot, make_message("средняя"))
    msg = session.last_message()
    assert texts.ASK_ACTIVITY in msg.text
    assert all(c.startswith("act:") for c in _callbacks(msg)) and len(_callbacks(msg)) == 5
    assert await get_fsm_state(dp) == Onboarding.activity.state


async def test_maintain_skips_target_step(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot, goal="maintain", target=None)
    assert await get_fsm_state(dp) is None
    user = await deps.storage.get_user(1)
    assert user.profile.goal == "maintain"
    assert user.profile.target_weight_kg is None
    assert "1 761" not in session.last_message().text
    assert "2 201" in session.last_message().text


async def test_lose_target_must_be_less(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, weight="70", activity="moderate", goal="lose")
    await dp.feed_update(bot, make_message("70"))
    assert session.last_message().text == texts.TARGET_LESS.format(weight="70")
    assert await get_fsm_state(dp) == Onboarding.target.state


async def test_gain_target_must_be_more(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, weight="70", activity="moderate", goal="gain")
    await dp.feed_update(bot, make_message("65"))
    assert session.last_message().text == texts.TARGET_MORE.format(weight="70")
    assert await get_fsm_state(dp) == Onboarding.target.state


async def test_teen_lose_switches_to_maintain(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, age="15", weight="60", activity="light")
    session.clear()
    await dp.feed_update(bot, make_callback("goal:lose"))
    joined = "\n".join(session.texts())
    assert "держать вес" in joined and "врач" in joined
    user = await deps.storage.get_user(1)
    assert user.profile.goal == "maintain"
    assert await get_fsm_state(dp) is None


async def test_low_bmi_target_warns_and_continue_finishes(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, weight="70", activity="moderate", goal="lose")
    await dp.feed_update(bot, make_message("48"))  # ИМТ 17,6 при 165 см
    msg = session.last_message()
    assert "18,5" in msg.text
    assert _callbacks(msg) == ["bmi:retry", "bmi:ok"]
    labels = [b.text for row in msg.reply_markup.inline_keyboard for b in row]
    assert labels == ["Выбрать другой вес", "Продолжить"]
    assert (await deps.storage.get_user(1)).profile is None

    await dp.feed_update(bot, make_callback("bmi:ok"))
    user = await deps.storage.get_user(1)
    assert user.profile.target_weight_kg == 48
    assert await get_fsm_state(dp) is None


async def test_low_bmi_retry_asks_target_again(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, weight="70", activity="moderate", goal="lose")
    await _feed(dp, bot, make_message("48"), make_callback("bmi:retry"))
    assert session.last_message().text == texts.ASK_TARGET
    assert await get_fsm_state(dp) == Onboarding.target.state
    await dp.feed_update(bot, make_message("60"))
    assert (await deps.storage.get_user(1)).profile.target_weight_kg == 60


async def test_free_text_after_onboarding_gets_fallback(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    await dp.feed_update(bot, make_message("привет"))
    assert session.last_message().text == texts.FALLBACK


async def test_start_again_after_onboarding_shows_menu(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    await dp.feed_update(bot, make_message("/start"))
    msg = session.last_message()
    assert msg.text == texts.WELCOME_BACK
    assert isinstance(msg.reply_markup, ReplyKeyboardMarkup)


async def test_goal_only_reentry_keeps_body_data(bot_env):
    """Повторный вход на шаг 6 (Task 10, «Новая цель»): шаги 1–5 не спрашиваются."""
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    user = await deps.storage.get_user(1)
    user.milestones_sent = {"first_kg"}
    await deps.storage.save_user(user)
    deps.clock.advance(timedelta(days=30))

    state = fsm_context(dp, 1)
    reply = await bot.send_message(1, "…")
    await start_goal_step(reply, state, user, weight_kg=66.0)
    assert session.last_message().text == texts.ASK_GOAL
    await _feed(dp, bot, make_callback("goal:lose"), make_message("60"))
    user = await deps.storage.get_user(1)
    assert user.profile.weight_kg == 66.0 and user.profile.target_weight_kg == 60
    assert user.profile.age == 30 and user.profile.height_cm == 165
    assert user.start_weight_kg == 66.0
    assert user.milestones_sent == set()
    assert user.onboarded_at == TODAY
    assert user.goal_started_at == date(2026, 11, 23)
    # Взвешивание не добавляется: вес тренда — не замер.
    assert [w.weight_kg for w in await deps.storage.weights(1)] == [70]
