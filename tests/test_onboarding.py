"""Старт без анкеты и профиль одной строкой (v1.1 3, 4; итог и безопасность — спец. 6.3, 7)."""
import io
from datetime import date, datetime, timedelta, timezone

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from PIL import Image

from bot import texts
from bot.handlers.onboarding import ProfileFlow, start_goal_step
from bot.models import Analysis, FoodItem
from tests.fakes import FakeVision, fsm_context, get_fsm_state, make_callback, make_message, onboard

TODAY = date(2026, 10, 24)
NOV2 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
HINT = (
    "Чтобы посчитать твою норму и остаток, напиши одной строкой: пол, год рождения "
    "(или возраст), рост, вес. Например: муж, 1987, 180, 87"
)


def _callbacks(msg: SendMessage) -> list[str]:
    assert isinstance(msg.reply_markup, InlineKeyboardMarkup)
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row]


async def _feed(dp, bot, *updates):
    for upd in updates:
        await dp.feed_update(bot, upd)


async def _consent(dp, bot):
    await _feed(dp, bot, make_message("/start"), make_callback("consent"))


async def _to_step(dp, bot, line="жен, 30, 165, 70", *, activity=None, goal=None):
    await _consent(dp, bot)
    await _feed(dp, bot, make_message(line), make_callback("pf:yes"))
    if activity:
        await _feed(dp, bot, make_callback(f"act:{activity}"))
    if goal:
        await _feed(dp, bot, make_callback(f"goal:{goal}"))


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


def _food(kcal: float = 165) -> Analysis:
    return Analysis(True, "high", (FoodItem("Гречка", 150, kcal, 6.0, 1.5, 33.0, False, "side"),), False)


def _with_photos(deps, results):
    deps.vision = FakeVision(list(results))

    async def fetch_photo(_bot, file_id):
        return _jpeg()

    deps.fetch_photo = fetch_photo


# --- /start ---


def test_profile_hint_wording():
    assert texts.PROFILE_HINT == HINT


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
    assert user.first_seen_at == deps.clock()


async def test_first_seen_kept_on_second_start(bot_env):
    dp, bot, session, deps = bot_env
    first = deps.clock()
    await dp.feed_update(bot, make_message("/start"))
    deps.clock.advance(timedelta(days=3))
    await dp.feed_update(bot, make_message("/start"))
    assert (await deps.storage.get_user(1)).first_seen_at == first


async def test_photo_before_consent_asks_consent_again(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    msg = session.last_message()
    assert msg.text == texts.CONSENT_REQUIRED
    assert _callbacks(msg) == ["consent"]
    assert await get_fsm_state(dp) is None


async def test_consent_shows_ready_without_questionnaire(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    user = await deps.storage.get_user(1)
    assert user.consent_at == deps.clock()
    msg = session.last_message()
    assert msg.text == texts.WELCOME_READY
    assert "Пришли фото еды" in msg.text
    assert isinstance(msg.reply_markup, ReplyKeyboardMarkup)
    assert await get_fsm_state(dp) is None
    assert user.profile is None


async def test_start_again_without_profile_shows_ready(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    await dp.feed_update(bot, make_message("/start"))
    assert session.last_message().text == texts.WELCOME_READY


# --- еда до профиля ---


async def test_photo_before_profile_is_recorded_with_hint_once_a_day(bot_env):
    dp, bot, session, deps = bot_env
    _with_photos(deps, [_food(), _food(), _food()])
    await _consent(dp, bot)
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    first = session.texts()[-1]
    assert "✅ Записал в дневник" in first
    assert HINT in first
    assert "Осталось на сегодня" not in first
    assert len(await deps.storage.meals_on(1, TODAY)) == 1
    assert (await deps.storage.get_user(1)).last_profile_hint_date == TODAY

    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    second = session.texts()[-1]
    assert "✅ Записал в дневник" in second
    assert HINT not in second and "Осталось на сегодня" not in second
    assert len(await deps.storage.meals_on(1, TODAY)) == 2

    deps.clock.advance(timedelta(days=1))
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert HINT in session.texts()[-1]


async def test_today_without_profile_shows_eaten_and_hint(bot_env):
    dp, bot, session, deps = bot_env
    _with_photos(deps, [_food(300)])
    await _consent(dp, bot)
    await dp.feed_update(bot, make_message(photo=True))
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    text = session.last_message().text
    assert text.startswith("📅 Сегодня — 300 ккал")
    assert "🍽 Обед — 300 ккал" in text
    assert "Белок 6 г" in text and "Жиры 2 г" in text and "Углеводы 33 г" in text
    assert "/" not in text.split("\n")[0]
    assert "можно ещё" not in text
    assert text.endswith(HINT)


async def test_weigh_and_profile_without_profile_give_hint(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    for button in (texts.BTN_WEIGH, texts.BTN_PROFILE):
        session.clear()
        await dp.feed_update(bot, make_message(button))
        assert session.texts() == [HINT]
        assert await get_fsm_state(dp) is None


# --- строка профиля ---


async def test_profile_line_asks_confirmation(bot_env):
    dp, bot, session, deps = bot_env
    deps.clock.set(NOV2)
    await _consent(dp, bot)
    await dp.feed_update(bot, make_message("муж, 1987, 180, 87"))
    msg = session.last_message()
    assert msg.text == "Мужчина, 39 лет, 180 см, 87 кг — верно?"
    assert _callbacks(msg) == ["pf:yes", "pf:no"]
    assert [b.text for row in msg.reply_markup.inline_keyboard for b in row] == ["Да", "Исправить"]
    assert await get_fsm_state(dp) == ProfileFlow.confirm.state
    assert (await deps.storage.get_user(1)).profile is None


async def test_profile_line_fix_asks_again(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    await _feed(dp, bot, make_message("муж, 1987, 180, 87"), make_callback("pf:no"))
    assert session.last_message().text == texts.PROFILE_RESEND
    assert "пол, год рождения (или возраст), рост, вес" in texts.PROFILE_RESEND
    assert await get_fsm_state(dp) is None
    await dp.feed_update(bot, make_message("муж, 1987, 182, 87"))
    assert "182 см" in session.last_message().text


async def test_partial_profile_line_gives_example_with_ranges(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    await dp.feed_update(bot, make_message("муж 1987"))
    text = session.last_message().text
    assert "муж, 1987, 180, 87" in text
    for fragment in ("1920", "2012", "14", "100", "120", "230", "30", "300"):
        assert fragment in text, fragment
    assert await get_fsm_state(dp) is None


async def test_non_profile_text_falls_through(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    await dp.feed_update(bot, make_message("привет"))
    assert session.last_message().text == texts.FALLBACK


async def test_full_reference_example(bot_env):
    dp, bot, session, deps = bot_env
    deps.clock.set(NOV2)
    today = date(2026, 11, 2)
    await _consent(dp, bot)
    await _feed(dp, bot, make_message("жен, 1996, 165, 70"))
    assert session.last_message().text == "Женщина, 30 лет, 165 см, 70 кг — верно?"
    await dp.feed_update(bot, make_callback("pf:yes"))
    msg = session.last_message()
    assert msg.text == texts.ASK_ACTIVITY and len(_callbacks(msg)) == 5
    await dp.feed_update(bot, make_callback("act:moderate"))
    msg = session.last_message()
    assert msg.text == texts.ASK_GOAL and _callbacks(msg) == ["goal:lose", "goal:maintain", "goal:gain"]
    await dp.feed_update(bot, make_callback("goal:lose"))
    assert session.last_message().text == texts.ASK_TARGET
    await dp.feed_update(bot, make_message("62"))

    msg = session.last_message()
    for fragment in ("1 420", "2 201", "1 761", "Белки 126 г", "Жиры 63 г", "Углеводы 173 г",
                     "2,1 л", "0,4 кг", "20 недел", "главное для сытости", "Отправь фото еды"):
        assert fragment in msg.text, fragment
    assert isinstance(msg.reply_markup, ReplyKeyboardMarkup)
    buttons = [b.text for row in msg.reply_markup.keyboard for b in row]
    assert buttons == [texts.BTN_TODAY, texts.BTN_WEIGH, texts.BTN_PROFILE, texts.BTN_HELP]

    user = await deps.storage.get_user(1)
    assert user.profile.age == 30 and user.profile.sex == "female"
    assert user.profile.target_weight_kg == 62
    assert user.profile.goal == "lose" and user.profile.activity == "moderate"
    assert round(user.norm.kcal) == 1761
    assert user.start_weight_kg == 70
    assert user.goal_started_at == today and user.onboarded_at == today
    assert [(w.day, w.weight_kg) for w in await deps.storage.weights(1)] == [(today, 70)]
    assert await get_fsm_state(dp) is None


async def test_onboard_helper_matches_reference(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    assert "1 761" in session.last_message().text
    assert round((await deps.storage.get_user(1)).norm.kcal) == 1761


async def test_weight_with_comma_and_unit(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, "жен, 30 лет, 165 см, 70,5 кг", activity="moderate", goal="maintain")
    user = await deps.storage.get_user(1)
    assert user.profile.weight_kg == 70.5
    assert user.start_weight_kg == 70.5


async def test_text_at_button_step_reasks_with_buttons(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot)
    session.clear()
    await dp.feed_update(bot, make_message("средняя"))
    msg = session.last_message()
    assert texts.ASK_ACTIVITY in msg.text and texts.USE_BUTTONS in msg.text
    assert all(c.startswith("act:") for c in _callbacks(msg)) and len(_callbacks(msg)) == 5
    assert await get_fsm_state(dp) == ProfileFlow.activity.state


async def test_text_at_confirm_reasks_confirmation(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    await dp.feed_update(bot, make_message("жен, 30, 165, 70"))
    session.clear()
    await dp.feed_update(bot, make_message("ага"))
    msg = session.last_message()
    assert msg.text.startswith(texts.USE_BUTTONS) and "165 см" in msg.text
    assert _callbacks(msg) == ["pf:yes", "pf:no"]
    assert await get_fsm_state(dp) == ProfileFlow.confirm.state


async def test_new_line_at_confirm_replaces_data(bot_env):
    dp, bot, session, deps = bot_env
    await _consent(dp, bot)
    await _feed(dp, bot, make_message("жен, 30, 165, 70"), make_message("жен, 30, 168, 70"))
    assert session.last_message().text == "Женщина, 30 лет, 168 см, 70 кг — верно?"
    assert await get_fsm_state(dp) == ProfileFlow.confirm.state


async def test_new_line_at_any_step_restarts_confirm(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot)
    await dp.feed_update(bot, make_message("жен, 30, 165, 72"))
    assert session.last_message().text == "Женщина, 30 лет, 165 см, 72 кг — верно?"
    assert await get_fsm_state(dp) == ProfileFlow.confirm.state

    await _feed(dp, bot, make_callback("pf:yes"), make_callback("act:moderate"), make_callback("goal:lose"))
    await dp.feed_update(bot, make_message("муж, 40, 180, 90"))
    assert session.last_message().text == "Мужчина, 40 лет, 180 см, 90 кг — верно?"
    assert await get_fsm_state(dp) == ProfileFlow.confirm.state
    assert (await deps.storage.get_user(1)).profile is None

    # Обычное число на шаге желаемого веса — по-прежнему желаемый вес.
    await _feed(dp, bot, make_callback("pf:yes"), make_callback("act:moderate"), make_callback("goal:lose"))
    await dp.feed_update(bot, make_message("80"))
    user = await deps.storage.get_user(1)
    assert user.profile.sex == "male" and user.profile.target_weight_kg == 80
    assert await get_fsm_state(dp) is None


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
    await _to_step(dp, bot, activity="moderate", goal="lose")
    await dp.feed_update(bot, make_message("70"))
    assert session.last_message().text == texts.TARGET_LESS.format(weight="70")
    assert await get_fsm_state(dp) == ProfileFlow.target.state


async def test_gain_target_must_be_more(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, activity="moderate", goal="gain")
    await dp.feed_update(bot, make_message("65"))
    assert session.last_message().text == texts.TARGET_MORE.format(weight="70")
    assert await get_fsm_state(dp) == ProfileFlow.target.state


async def test_teen_lose_switches_to_maintain(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, "жен, 15, 165, 60", activity="light")
    session.clear()
    await dp.feed_update(bot, make_callback("goal:lose"))
    joined = "\n".join(session.texts())
    assert "держать вес" in joined and "врач" in joined
    user = await deps.storage.get_user(1)
    assert user.profile.goal == "maintain"
    assert await get_fsm_state(dp) is None


async def test_low_bmi_target_warns_and_continue_finishes(bot_env):
    dp, bot, session, deps = bot_env
    await _to_step(dp, bot, activity="moderate", goal="lose")
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
    await _to_step(dp, bot, activity="moderate", goal="lose")
    await _feed(dp, bot, make_message("48"), make_callback("bmi:retry"))
    assert session.last_message().text == texts.ASK_TARGET
    assert await get_fsm_state(dp) == ProfileFlow.target.state
    await dp.feed_update(bot, make_message("60"))
    assert (await deps.storage.get_user(1)).profile.target_weight_kg == 60


# --- профиль уже есть ---


async def test_repeat_line_with_profile_changes_goal_only_after_summary(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    user = await deps.storage.get_user(1)
    user.milestones_sent = {"first_kg"}
    await deps.storage.save_user(user)
    deps.clock.advance(timedelta(days=10))
    later = TODAY + timedelta(days=10)

    await _feed(
        dp, bot,
        make_message("жен, 30, 165, 68"),
        make_callback("pf:yes"),
        make_callback("act:light"),
        make_callback("goal:lose"),
    )
    user = await deps.storage.get_user(1)
    assert user.profile.target_weight_kg == 62 and user.profile.activity == "moderate"
    assert user.start_weight_kg == 70 and user.milestones_sent == {"first_kg"}
    assert user.goal_started_at == TODAY

    await dp.feed_update(bot, make_message("60"))
    user = await deps.storage.get_user(1)
    assert user.profile.target_weight_kg == 60 and user.profile.activity == "light"
    assert user.profile.weight_kg == 68 and user.start_weight_kg == 68
    assert user.milestones_sent == set()
    assert user.goal_started_at == later and user.onboarded_at == TODAY
    assert [(w.day, w.weight_kg) for w in await deps.storage.weights(1)] == [(TODAY, 70), (later, 68)]


async def test_free_text_after_profile_gets_fallback(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    await dp.feed_update(bot, make_message("привет"))
    assert session.last_message().text == texts.FALLBACK


async def test_start_again_after_profile_shows_menu(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    await dp.feed_update(bot, make_message("/start"))
    msg = session.last_message()
    assert msg.text == texts.WELCOME_BACK
    assert isinstance(msg.reply_markup, ReplyKeyboardMarkup)


async def test_goal_only_reentry_keeps_body_data(bot_env):
    """«Новая цель» / «Изменить цель»: сразу шаг цели, данные тела из профиля."""
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
    assert await get_fsm_state(dp) == ProfileFlow.goal.state
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
