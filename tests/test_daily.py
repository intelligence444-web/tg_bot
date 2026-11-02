"""Ежедневные рассылки: напоминание о взвешивании, /stop_tips (спец. 9.3, 11, 13)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot import texts
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


# --- курса больше нет ---


async def test_no_course_cards(env):
    storage, bot, session = env
    tue = MON + timedelta(days=1)
    await storage.save_user(_user(onboarded=tue - timedelta(days=3)))
    await storage.upsert_weight(1, tue - timedelta(days=1), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(tue))
    assert counts == {"reminders": 0, "deactivated": 0}
    assert _sent(session) == []
    assert (await storage.get_user(1)).course_day == 0


# --- напоминание о взвешивании ---


async def test_monday_reminder_when_last_weighing_8_days_ago(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    await storage.upsert_weight(1, MON - timedelta(days=8), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"reminders": 1, "deactivated": 0}
    assert [m.text for m in _sent(session)] == [texts.WEIGH_REMINDER]


@pytest.mark.parametrize("days_ago", [3, 7])
async def test_no_monday_reminder_when_weighed_recently(env, days_ago):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    await storage.upsert_weight(1, MON - timedelta(days=days_ago), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts["reminders"] == 0
    assert _sent(session) == []


async def test_no_reminder_when_not_monday(env):
    storage, bot, session = env
    tue = MON + timedelta(days=1)
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    await storage.upsert_weight(1, tue - timedelta(days=30), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(tue))
    assert counts["reminders"] == 0
    assert _sent(session) == []


async def test_monday_is_by_moscow_date(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    await storage.upsert_weight(1, MON - timedelta(days=10), 70.0)
    # воскресенье 22:00 UTC — уже понедельник 01:00 по Москве
    counts = await run_daily(storage, bot, datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc))
    assert counts["reminders"] == 1


async def test_monday_reminder_without_weighings(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    counts = await run_daily(storage, bot, at_9_msk(MON))  # взвешиваний нет вовсе
    assert counts == {"reminders": 1, "deactivated": 0}
    assert [m.text for m in _sent(session)] == [texts.WEIGH_REMINDER]


async def test_tips_disabled_gets_nothing(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), tips_enabled=False))
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"reminders": 0, "deactivated": 0}
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
    assert u1.is_active is False
    assert u2.is_active is True
    assert counts["reminders"] == 1  # только второму

    session.clear()
    await run_daily(storage, bot, at_9_msk(MON + timedelta(days=7)))
    assert {m.chat_id for m in _sent(session)} == {2}


async def test_other_telegram_error_does_not_stop_run(env):
    storage, bot, session = env
    old = MON - timedelta(days=20)
    await storage.save_user(_user(1, onboarded=old))
    await storage.save_user(_user(2, onboarded=old))
    session.fail(SendMessage, TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="Bad"), times=1)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"reminders": 1, "deactivated": 0}
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
    assert counts == {"reminders": 0, "deactivated": 0}
    assert _sent(session) == []
    assert (await storage.get_user(1)).tips_enabled is False


class _BrokenForOneStorage(MemoryStorage):
    async def get_user(self, user_id):
        if user_id == 1:
            raise RuntimeError("ydb down")
        return await super().get_user(user_id)


async def test_storage_error_for_one_user_does_not_abort_run(caplog):
    storage = _BrokenForOneStorage()
    session = MockedSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    old = MON - timedelta(days=20)
    await storage.save_user(_user(1, onboarded=old))
    await storage.save_user(_user(2, onboarded=old))
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"reminders": 1, "deactivated": 0}
    assert {m.chat_id for m in _sent(session)} == {2}
    [rec] = [r for r in caplog.records if r.name == "bot.daily" and r.exc_info]
    assert rec.user_id == 1 and rec.error_kind == "daily"
