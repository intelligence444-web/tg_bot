"""Ежедневные рассылки: напоминание о взвешивании, недельный отчёт, /stop_tips (спец. 9.3, 11, 13; v1.1 10)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot import texts
from bot.calc import calculate_norm
from bot.daily import run_daily
from bot.models import Profile, User
from bot.storage import MemoryStorage
from bot.vision import VisionUnavailable
from tests.fakes import BOT_TOKEN, FakeVision, MockedSession, get_fsm_state, make_callback, make_message, onboard
from tests.test_weekly import meal

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
    assert counts == {"reminders": 0, "deactivated": 0, "reports": 0}
    assert _sent(session) == []
    assert (await storage.get_user(1)).course_day == 0


# --- напоминание о взвешивании ---


async def test_monday_reminder_when_last_weighing_8_days_ago(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20)))
    await storage.upsert_weight(1, MON - timedelta(days=8), 70.0)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"reminders": 1, "deactivated": 0, "reports": 0}
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
    assert counts == {"reminders": 1, "deactivated": 0, "reports": 0}
    assert [m.text for m in _sent(session)] == [texts.WEIGH_REMINDER]


async def test_tips_disabled_gets_nothing(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=20), tips_enabled=False))
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts == {"reminders": 0, "deactivated": 0, "reports": 0}
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
    assert counts == {"reminders": 1, "deactivated": 0, "reports": 0}
    assert [(await storage.get_user(i)).is_active for i in (1, 2)] == [True, True]


# --- возврат активности ---


async def test_message_from_inactive_user_reactivates(bot_env):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    user = await deps.storage.get_user(1)
    user.is_active = False
    await deps.storage.save_user(user)
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
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


async def test_commands_mid_profile_steps_keep_state(bot_env):
    dp, bot, session, deps = bot_env
    await dp.feed_update(bot, make_message("/start"))
    await dp.feed_update(bot, make_callback("consent"))
    await dp.feed_update(bot, make_message("жен, 30, 165, 70"))
    state = await get_fsm_state(dp)
    assert state is not None
    session.clear()
    await dp.feed_update(bot, make_message(texts.BTN_HELP))
    await dp.feed_update(bot, make_message("/stop_tips"))
    assert session.texts() == [texts.HELP, texts.TIPS_STOPPED]
    assert await get_fsm_state(dp) == state
    assert (await deps.storage.get_user(1)).tips_enabled is False
    session.clear()
    await dp.feed_update(bot, make_callback("pf:yes"))  # шаги профиля продолжаются
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
    assert counts == {"reminders": 0, "deactivated": 0, "reports": 0}
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
    assert counts == {"reminders": 1, "deactivated": 0, "reports": 0}
    assert {m.chat_id for m in _sent(session)} == {2}
    recs = [r for r in caplog.records if r.name == "bot.daily" and r.exc_info]
    assert recs  # по записи на каждый шаг рассылки (напоминание, отчёт)
    assert all(r.user_id == 1 and r.error_kind == "daily" for r in recs)


# --- недельный отчёт (v1.1 10) ---

_PROFILE = Profile("female", 30, 165.0, 70.0, "moderate", "lose", 62.0)


async def _report_user(storage, days_logged: int, **kw) -> None:
    await storage.save_user(
        _user(onboarded=MON - timedelta(days=30), profile=_PROFILE, norm=calculate_norm(_PROFILE), **kw)
    )
    await storage.upsert_weight(1, MON - timedelta(days=2), 70.0)  # без напоминания о весе
    for i in range(days_logged):
        await storage.add_meal(meal(MON - timedelta(days=7 - i), 1500, 80))


async def test_monday_weekly_report_with_3_days(env):
    storage, bot, session = env
    await _report_user(storage, 3)
    chat = FakeVision([], texts=["Добавь овощей к ужину."])
    counts = await run_daily(storage, bot, at_9_msk(MON), chat)
    assert counts == {"reminders": 0, "deactivated": 0, "reports": 1}
    [msg] = _sent(session)
    assert msg.text.startswith(texts.WEEKLY_TITLE)
    assert "3 из 7" in msg.text and "Добавь овощей к ужину." in msg.text
    assert not isinstance(msg.parse_mode, str)  # простой текст
    assert len(chat.completions) == 1
    assert await storage.incr_ai_usage(1, MON) == 1  # лимит ИИ не тронут


async def test_weekly_report_after_weigh_reminder(env):
    storage, bot, session = env
    await _report_user(storage, 3)
    storage._weights.clear()
    counts = await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=["Совет."]))
    assert counts == {"reminders": 1, "deactivated": 0, "reports": 1}
    texts_sent = [m.text for m in _sent(session)]
    assert texts_sent[0] == texts.WEIGH_REMINDER and texts_sent[1].startswith(texts.WEEKLY_TITLE)


async def test_no_weekly_report_with_2_days(env):
    storage, bot, session = env
    await _report_user(storage, 2)
    chat = FakeVision([], texts=["Совет."])
    counts = await run_daily(storage, bot, at_9_msk(MON), chat)
    assert counts["reports"] == 0 and _sent(session) == [] and chat.completions == []


async def test_no_weekly_report_on_tuesday(env):
    storage, bot, session = env
    await _report_user(storage, 5)
    counts = await run_daily(storage, bot, at_9_msk(MON + timedelta(days=1)), FakeVision([], texts=["Совет."]))
    assert counts["reports"] == 0 and _sent(session) == []


async def test_no_weekly_report_without_profile(env):
    storage, bot, session = env
    await storage.save_user(_user(onboarded=MON - timedelta(days=30)))
    await storage.upsert_weight(1, MON - timedelta(days=2), 70.0)
    for i in range(5):
        await storage.add_meal(meal(MON - timedelta(days=7 - i), 1500, 80))
    counts = await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=["Совет."]))
    assert counts["reports"] == 0 and _sent(session) == []


async def test_no_weekly_report_with_tips_disabled(env):
    storage, bot, session = env
    await _report_user(storage, 5, tips_enabled=False)
    counts = await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=["Совет."]))
    assert counts["reports"] == 0 and _sent(session) == []


async def test_weekly_report_without_advice_when_model_fails(env):
    storage, bot, session = env
    await _report_user(storage, 4)
    counts = await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=[VisionUnavailable("down")]))
    assert counts["reports"] == 1
    [msg] = _sent(session)
    assert msg.text.startswith(texts.WEEKLY_TITLE) and "Совет" not in msg.text


async def test_weekly_report_without_chat_client(env):
    storage, bot, session = env
    await _report_user(storage, 4)
    counts = await run_daily(storage, bot, at_9_msk(MON))
    assert counts["reports"] == 1
    assert "Совет" not in _sent(session)[0].text


async def test_weekly_report_forbidden_deactivates(env):
    storage, bot, session = env
    await _report_user(storage, 4)
    session.fail(SendMessage, _forbidden())
    counts = await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=["Совет."]))
    assert counts == {"reminders": 0, "deactivated": 1, "reports": 0}
    assert (await storage.get_user(1)).is_active is False


class _StopTipsDuringAdvice(FakeVision):
    def __init__(self, storage):
        super().__init__([], texts=["Совет."])
        self._storage = storage

    async def complete(self, system, messages, max_tokens=1500):
        u = await self._storage.get_user(1)
        u.tips_enabled = False
        await self._storage.save_user(u)
        return await super().complete(system, messages, max_tokens)


async def test_weekly_report_rereads_user_before_send(env):
    storage, bot, session = env
    await _report_user(storage, 4)
    counts = await run_daily(storage, bot, at_9_msk(MON), _StopTipsDuringAdvice(storage))
    assert counts["reports"] == 0 and _sent(session) == []


async def test_daily_build_chat(monkeypatch, caplog):
    import httpx  # noqa: PLC0415

    from bot import daily  # noqa: PLC0415

    monkeypatch.setenv("KIE_API_KEY", "k")
    monkeypatch.setenv("VISION_PRIMARY", "openai:gemini-3-pro")
    monkeypatch.delenv("VISION_FALLBACK", raising=False)
    monkeypatch.delenv("CHAT_PRIMARY", raising=False)
    monkeypatch.setenv("CHAT_FALLBACK", "openai:gemini-2.5-pro")
    async with httpx.AsyncClient() as http:
        client = daily.build_chat(http)
        assert client.primary.model == "gemini-3-pro" and client.fallback.model == "gemini-2.5-pro"

        monkeypatch.setenv("CHAT_PRIMARY", "claude:x")
        monkeypatch.delenv("CHAT_FALLBACK")
        client = daily.build_chat(http)
        assert client.primary.model == "x" and client.fallback is None

        monkeypatch.delenv("KIE_API_KEY")
        caplog.clear()
        assert daily.build_chat(http) is None
        assert any(r.levelname == "WARNING" and getattr(r, "error_kind", None) == "config" for r in caplog.records)

        monkeypatch.setenv("KIE_API_KEY", "k")
        monkeypatch.delenv("CHAT_PRIMARY")
        monkeypatch.delenv("VISION_PRIMARY")
        assert daily.build_chat(http) is None


# --- порядок, бюджет, параллельность и повторный запуск ---


async def _report_users(storage, ids, *, weighed=True):
    for uid in ids:
        await storage.save_user(
            _user(uid, onboarded=MON - timedelta(days=30), profile=_PROFILE, norm=calculate_norm(_PROFILE))
        )
        if weighed:
            await storage.upsert_weight(uid, MON - timedelta(days=2), 70.0)
        for i in range(3):
            m = meal(MON - timedelta(days=7 - i), 1500, 80)
            m.user_id = uid
            m.meal_id = f"{uid}-{m.meal_id}"
            await storage.add_meal(m)


async def test_all_reminders_go_before_reports(env):
    storage, bot, session = env
    await _report_users(storage, [1, 2], weighed=False)
    counts = await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=["Совет.", "Совет."]))
    assert counts == {"reminders": 2, "deactivated": 0, "reports": 2}
    sent = [m.text for m in _sent(session)]
    assert sent[:2] == [texts.WEIGH_REMINDER, texts.WEIGH_REMINDER]
    assert all(t.startswith(texts.WEEKLY_TITLE) for t in sent[2:])


async def test_rerun_same_day_does_not_resend(env):
    storage, bot, session = env
    await _report_users(storage, [1], weighed=False)
    await run_daily(storage, bot, at_9_msk(MON), FakeVision([], texts=["Совет."]))
    user = await storage.get_user(1)
    assert user.last_report_date == MON and user.last_weigh_reminder_date == MON
    session.clear()
    chat = FakeVision([], texts=["Совет."])
    counts = await run_daily(storage, bot, at_9_msk(MON), chat)
    assert counts == {"reminders": 0, "deactivated": 0, "reports": 0}
    assert _sent(session) == [] and chat.completions == []


async def test_advice_budget_spent_reports_without_advice(env, monkeypatch):
    from bot import daily  # noqa: PLC0415

    storage, bot, session = env
    await _report_users(storage, [1, 2])
    monkeypatch.setattr(daily, "ADVICE_TOTAL_BUDGET_S", 0.0)
    chat = FakeVision([], texts=["Совет.", "Совет."])
    counts = await run_daily(storage, bot, at_9_msk(MON), chat)
    assert counts["reports"] == 2
    assert chat.completions == []
    assert all("Совет" not in m.text for m in _sent(session))


class _SlowChat(FakeVision):
    def __init__(self, n: int, delay: float):
        super().__init__([], texts=["Совет."] * n)
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, system, messages, max_tokens=1500):
        import asyncio  # noqa: PLC0415

        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return await super().complete(system, messages, max_tokens)
        finally:
            self.in_flight -= 1


async def test_advice_runs_with_limited_concurrency(env, monkeypatch):
    from bot import daily  # noqa: PLC0415

    storage, bot, session = env
    monkeypatch.setattr(daily, "SEND_PAUSE_S", 0)
    await _report_users(storage, range(1, 9))
    chat = _SlowChat(8, 0.05)
    counts = await run_daily(storage, bot, at_9_msk(MON), chat)
    assert counts["reports"] == 8
    assert chat.max_in_flight == daily.ADVICE_CONCURRENCY == 5
    assert all("Совет." in m.text for m in _sent(session))


async def test_advice_budget_runs_out_mid_run(env, monkeypatch):
    from bot import daily  # noqa: PLC0415

    storage, bot, session = env
    monkeypatch.setattr(daily, "SEND_PAUSE_S", 0)
    monkeypatch.setattr(daily, "ADVICE_TOTAL_BUDGET_S", 0.05)
    await _report_users(storage, range(1, 9))
    chat = _SlowChat(8, 0.5)  # дольше бюджета: первые советы обрываются, остальные не запрашиваются
    counts = await run_daily(storage, bot, at_9_msk(MON), chat)
    assert counts["reports"] == 8
    assert chat.max_in_flight <= 5 and chat.completions == []  # оборваны по сроку
    assert all("Совет" not in m.text for m in _sent(session))
