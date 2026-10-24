"""Ежедневные рассылки: курс «Знание дня», напоминание о взвешивании, /stop_tips (спец. 9.3, 11, 13)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot import course, texts
from bot.daily import run_daily
from bot.models import User
from bot.storage import MemoryStorage
from tests.fakes import BOT_TOKEN, MockedSession, get_fsm_state, make_callback, make_message, onboard

SAT = date(2026, 10, 24)
MON = date(2026, 10, 26)


def at_9_msk(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 6, 0, tzinfo=timezone.utc)


def _user(user_id: int = 1, onboarded: date = SAT, **kw) -> User:
    return User(
        user_id=user_id,
        consent_at=datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc),
        onboarded_at=onboarded,
        **kw,
    )


@pytest.fixture
async def env():
    storage = MemoryStorage()
    session = MockedSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    yield storage, bot, session
    await bot.session.close()


def _sent(session) -> list[SendMessage]:
    return session.of_type(SendMessage)


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked by the user")


# --- курс ---


async def test_card_not_sent_on_onboarding_day_then_sent_next_day(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=SAT))
    await storage.upsert_weight(1, SAT, 70.0)

    counts = await run_daily(storage, bot, at_9_msk(SAT))
    assert counts == {"cards": 0, "reminders": 0, "deactivated": 0}
    assert _sent(session) == []
    assert (await storage.get_user(1)).course_day == 0

    counts = await run_daily(storage, bot, at_9_msk(SAT + timedelta(days=1)))
    assert counts["cards"] == 1
    [msg] = _sent(session)
    assert msg.chat_id == 1
    assert msg.parse_mode == "HTML"
    card = course.card_for(1)
    assert msg.text == course.format_card(1, card)
    assert msg.text.startswith("📚 Знание дня · 1/30\n\n<b>")
    assert (await storage.get_user(1)).course_day == 1


async def test_course_day_grows_each_day(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=SAT))
    await storage.upsert_weight(1, SAT, 70.0)
    for i in range(1, 3):
        await run_daily(storage, bot, at_9_msk(SAT + timedelta(days=i)))
        assert (await storage.get_user(1)).course_day == i
    assert [m.text for m in _sent(session)] == [course.format_card(i, course.card_for(i)) for i in (1, 2)]


async def test_nothing_after_last_existing_card(env):
    storage, bot, session = env
    last = len(course.CARDS)
    await storage.save_user(_user(onboarded=SAT, course_day=last))
    await storage.upsert_weight(1, SAT, 70.0)
    counts = await run_daily(storage, bot, at_9_msk(SAT + timedelta(days=1)))
    assert counts["cards"] == 0
    assert _sent(session) == []
    assert (await storage.get_user(1)).course_day == last
    assert course.card_for(last + 1) is None
    assert course.card_for(0) is None


# --- напоминание о взвешивании ---


async def test_monday_reminder_when_last_weighing_8_days_ago(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), course_day=len(course.CARDS)))
    await storage.upsert_weight(1, MON - timedelta(days=8), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"cards": 0, "reminders": 1, "deactivated": 0}
    assert [m.text for m in _sent(session)] == [texts.WEIGH_REMINDER]


@pytest.mark.parametrize("days_ago", [3, 7])
async def test_no_monday_reminder_when_weighed_recently(env, days_ago):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), course_day=len(course.CARDS)))
    await storage.upsert_weight(1, MON - timedelta(days=days_ago), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts["reminders"] == 0
    assert _sent(session) == []


async def test_no_reminder_when_not_monday(env):
    storage, bot, session = env
    tue = MON + timedelta(days=1)
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), course_day=len(course.CARDS)))
    await storage.upsert_weight(1, tue - timedelta(days=30), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(tue))
    assert counts["reminders"] == 0
    assert _sent(session) == []


async def test_monday_is_by_moscow_date(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), course_day=len(course.CARDS)))
    await storage.upsert_weight(1, MON - timedelta(days=10), 70.0)
    # воскресенье 22:00 UTC — уже понедельник 01:00 по Москве
    counts = await run_daily(storage, bot, datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc))
    assert counts["reminders"] == 1


async def test_card_and_reminder_on_monday(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    counts = await run_daily(storage, bot, at_9_msk(MON))  # взвешиваний нет вовсе
    assert counts == {"cards": 1, "reminders": 1, "deactivated": 0}
    assert [m.text for m in _sent(session)] == [course.format_card(1, course.card_for(1)), texts.WEIGH_REMINDER]


async def test_tips_disabled_gets_nothing(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), tips_enabled=False))
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"cards": 0, "reminders": 0, "deactivated": 0}
    assert _sent(session) == []
    assert (await storage.get_user(1)).course_day == 0


# --- 403 и прочие ошибки Telegram ---


async def test_forbidden_deactivates_and_next_run_skips(env):
    storage, bot, session = env
    await storage.save_user(_user(1, onboarded=MON - timedelta(days=20)))
    await storage.save_user(_user(2, onboarded=MON - timedelta(days=20)))
    session.fail(SendMessage, _forbidden(), times=1)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts["deactivated"] == 1
    u1, u2 = await storage.get_user(1), await storage.get_user(2)
    assert u1.is_active is False and u1.course_day == 0
    assert u2.is_active is True and u2.course_day == 1
    assert counts["cards"] == 1 and counts["reminders"] == 1  # только второму

    session.clear()
    await run_daily(storage, bot, at_9_msk(MON + timedelta(days=1)))
    assert {m.chat_id for m in _sent(session)} == {2}


async def test_other_telegram_error_does_not_stop_run(env):
    storage, bot, session = env
    await storage.save_user(_user(1, onboarded=SAT))
    await storage.save_user(_user(2, onboarded=SAT))
    await storage.upsert_weight(1, SAT, 70.0)
    await storage.upsert_weight(2, SAT, 70.0)
    session.fail(SendMessage, TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="Bad"), times=1)
    counts = await run_daily(storage, bot, at_9_msk(SAT + timedelta(days=1)))
    assert counts == {"cards": 1, "reminders": 0, "deactivated": 0}
    days = sorted([(await storage.get_user(i)).course_day for i in (1, 2)])
    assert days == [0, 1]
    assert [(await storage.get_user(i)).is_active for i in (1, 2)] == [True, True]


# --- возврат активности ---


async def test_message_from_inactive_user_reactivates(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    user = await deps.storage.get_user(1)
    user.is_active = False
    await deps.storage.save_user(user)
    await dp.feed_update(bot, make_message("привет"))
    assert (await deps.storage.get_user(1)).is_active is True


async def test_callback_from_inactive_user_reactivates(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    user = await deps.storage.get_user(1)
    user.is_active = False
    await deps.storage.save_user(user)
    await dp.feed_update(bot, make_callback("stale"))
    assert (await deps.storage.get_user(1)).is_active is True


async def test_reactivation_does_not_create_users(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("привет", user_id=7))
    assert await deps.storage.get_user(7) is None


# --- /stop_tips, /start_tips, «❓ Помощь» ---


async def test_stop_and_start_tips(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    session.clear()
    await dp.feed_update(bot, make_message("/stop_tips"))
    assert (await deps.storage.get_user(1)).tips_enabled is False
    assert session.texts() == [texts.TIPS_STOPPED]
    await dp.feed_update(bot, make_message("/start_tips"))
    assert (await deps.storage.get_user(1)).tips_enabled is True
    assert session.texts()[-1] == texts.TIPS_STARTED


async def test_help_button(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_HELP))
    assert session.texts() == [texts.HELP]
    assert "/stop_tips" in texts.HELP


async def test_commands_mid_questionnaire_keep_state(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    await dp.feed_update(bot, make_callback("consent"))
    await dp.feed_update(bot, make_callback("sex:female"))
    state = await get_fsm_state(dp)
    assert state is not None
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_HELP))
    await dp.feed_update(bot, make_message("/stop_tips"))
    assert session.texts() == [texts.HELP, texts.TIPS_STOPPED]
    assert await get_fsm_state(dp) == state
    assert (await deps.storage.get_user(1)).tips_enabled is False
    session.clear()
    await dp.feed_update(bot, make_message("30"))  # анкета продолжается
    assert await get_fsm_state(dp) != state


async def test_commands_before_consent_hit_consent_gate(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    session.clear()
    await dp.feed_update(bot, make_message("/stop_tips"))
    await dp.feed_update(bot, make_message(texts.BTN_HELP))
    assert session.texts() == [texts.CONSENT_REQUIRED, texts.CONSENT_REQUIRED]
    assert (await deps.storage.get_user(1)).tips_enabled is True


# --- карточки ---

_BANNED = re.compile(r"нельзя|запрещ|сорвал|плохо", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def test_cards_valid():
    assert course.CARDS, "нужна хотя бы одна карточка"
    assert len(course.CARDS) <= course.COURSE_LENGTH == 30
    for n, card in enumerate(course.CARDS, start=1):
        assert card.title.strip(), n
        assert card.body.strip() and card.tip.strip(), n
        assert len(card.body) + len(card.tip) <= 600, n
        for part in (card.title, card.body, card.tip):
            assert not _BANNED.search(part), (n, part)
            assert not _EMOJI.search(part), (n, part)  # в шаблоне уже 2 эмодзи
        assert course.card_for(n) is card


def test_format_card_escapes_html():
    card = course.Card(title="A < B", body="x & y", tip="<i>")
    text = course.format_card(3, card)
    assert text == "📚 Знание дня · 3/30\n\n<b>A &lt; B</b>\n\nx &amp; y\n\n💡 &lt;i&gt;"


# --- свежие данные и ошибки по одному пользователю ---


class _FlipTipsStorage(MemoryStorage):
    """После выдачи списка рассылки пользователь успевает нажать /stop_tips."""

    async def daily_users(self):
        users = await super().daily_users()
        u = await self.get_user(1)
        u.tips_enabled = False
        await self.save_user(u)
        return users


async def test_stop_tips_after_list_is_respected():
    storage = _FlipTipsStorage()
    session = MockedSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    await storage.save_user(_user(1, onboarded=MON - timedelta(days=20)))
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"cards": 0, "reminders": 0, "deactivated": 0}
    assert _sent(session) == []
    user = await storage.get_user(1)
    assert user.tips_enabled is False and user.course_day == 0


class _BrokenForOneStorage(MemoryStorage):
    async def get_user(self, user_id):
        if user_id == 1:
            raise RuntimeError("ydb down")
        return await super().get_user(user_id)


async def test_storage_error_for_one_user_does_not_abort_run(caplog):
    storage = _BrokenForOneStorage()
    session = MockedSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    await storage.save_user(_user(1, onboarded=SAT))
    await storage.save_user(_user(2, onboarded=SAT))
    await storage.upsert_weight(2, SAT, 70.0)
    counts = await run_daily(storage, bot, at_9_msk(SAT + timedelta(days=1)))
    assert counts == {"cards": 1, "reminders": 0, "deactivated": 0}
    assert {m.chat_id for m in _sent(session)} == {2}
    [rec] = [r for r in caplog.records if r.name == "bot.daily" and r.exc_info]
    assert rec.user_id == 1 and rec.error_kind == "daily"
