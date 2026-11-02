"""Голосовые сообщения (v1.1): SpeechKit → handle_text."""
from __future__ import annotations

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import GetFile

from bot import texts
from bot.handlers.meals import MealEdit
from bot.handlers.weight import WeightInput
from bot.speech import SpeechError
from tests.fakes import FakeSpeech, FakeVision, fsm_context, get_fsm_state, make_message, onboard
from tests.test_text import EGGS, TODAY, chat_json, food_json


async def _setup(bot_env, speech, replies=()):
    dp, bot, session, deps = bot_env
    deps.vision = FakeVision([], texts=list(replies))
    deps.speech = speech
    await onboard(dp, bot)
    session.clear()
    return dp, bot, session, deps


async def _ai_used(deps) -> int:
    return deps.storage._ai_usage.get((1, TODAY), 0)


async def test_30s_voice_recognized_and_recorded_as_food(bot_env):
    speech = FakeSpeech(["съел два яйца и тост"])
    dp, bot, session, deps = await _setup(bot_env, speech, [food_json()])
    await dp.feed_update(bot, make_message(voice=30))
    assert len(speech.calls) == 1
    out = session.texts()[-1]
    assert out.startswith("🎙 Понял так: съел два яйца и тост")
    assert "Яйцо варёное" in out
    assert len(await deps.storage.meals_on(1, TODAY)) == 1
    assert await _ai_used(deps) == 1


async def test_prefix_escaped_in_html_reply(bot_env):
    speech = FakeSpeech(["а < б & в"])
    dp, bot, session, deps = await _setup(bot_env, speech, [chat_json("Ок")])
    await dp.feed_update(bot, make_message(voice=5))
    msg = session.last_message()
    assert msg.parse_mode == "HTML"
    assert msg.text.startswith("🎙 Понял так: а &lt; б &amp; в\n\n")


async def test_31s_voice_too_long_no_speechkit(bot_env):
    speech = FakeSpeech(["x"])
    dp, bot, session, deps = await _setup(bot_env, speech)
    await dp.feed_update(bot, make_message(voice=31))
    assert session.texts() == [texts.VOICE_TOO_LONG] == ["Скажи покороче — до 30 секунд"]
    assert speech.calls == []
    assert await _ai_used(deps) == 0


async def test_empty_recognition(bot_env):
    dp, bot, session, deps = await _setup(bot_env, FakeSpeech(["  "]))
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_EMPTY] == ["Не расслышал, попробуй ещё раз или напиши текстом"]
    assert await _ai_used(deps) == 0


async def test_speech_error(bot_env):
    dp, bot, session, deps = await _setup(bot_env, FakeSpeech([SpeechError("boom")]))
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_FAILED] == ["Не получилось распознать голос, напиши текстом"]
    assert await _ai_used(deps) == 0


async def test_no_speech_configured(bot_env):
    dp, bot, session, deps = await _setup(bot_env, None)
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_FAILED] == ["Не получилось распознать голос, напиши текстом"]


async def test_video_note_and_audio(bot_env):
    speech = FakeSpeech([])
    dp, bot, session, deps = await _setup(bot_env, speech)
    await dp.feed_update(bot, make_message(video_note=True))
    await dp.feed_update(bot, make_message(audio=True))
    assert session.texts() == [texts.VOICE_ONLY] * 2
    assert texts.VOICE_ONLY == "Пришли голосовое или напиши текстом"
    assert speech.calls == []


async def test_recognition_alone_spends_no_ai_unit(bot_env):
    # шаблон «вес» разбирается кодом: распознавание и обработка без обращения к модели
    speech = FakeSpeech(["вес 85,5"])
    dp, bot, session, deps = await _setup(bot_env, speech)
    await dp.feed_update(bot, make_message(voice=4))
    assert len(speech.calls) == 1
    assert await _ai_used(deps) == 0
    assert deps.vision.completions == []


async def test_voice_in_fsm_state_asks_text(bot_env):
    speech = FakeSpeech(["x"])
    dp, bot, session, deps = await _setup(bot_env, speech)
    await fsm_context(dp).set_state(MealEdit.correction)
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_IN_STATE]
    assert speech.calls == []


async def test_download_failure_gives_voice_failed(bot_env):
    for exc in (TelegramAPIError(GetFile(file_id="x"), "boom"), OSError("net")):
        speech = FakeSpeech(["x"])
        dp, bot, session, deps = await _setup(bot_env, speech)

        async def broken(_bot, _file_id, exc=exc):
            raise exc

        deps.fetch_photo = broken
        await dp.feed_update(bot, make_message(voice=5))
        assert session.texts() == [texts.VOICE_FAILED]
        assert speech.calls == []
        assert await _ai_used(deps) == 0


async def test_voice_in_weight_input_clears_state_and_is_processed_as_text(bot_env):
    # как и текст: выход из ввода веса сбрасывает состояние и сообщение идёт дальше
    speech = FakeSpeech(["привет"])
    dp, bot, session, deps = await _setup(bot_env, speech, [chat_json("Привет!")])
    await fsm_context(dp).set_state(WeightInput.value)
    await dp.feed_update(bot, make_message(voice=3))
    assert await get_fsm_state(dp) is None
    assert len(speech.calls) == 1
    assert session.texts()[-1].startswith("🎙 Понял так: привет")


# --- финальное ревью v1.1: общий срок голоса и префикс в шаблонах ---


class _SlowSpeech(FakeSpeech):
    def __init__(self, results, delay: float):
        super().__init__(results)
        self.delay = delay

    async def recognize(self, ogg_opus: bytes) -> str:
        import asyncio

        await asyncio.sleep(self.delay)
        return await super().recognize(ogg_opus)


async def test_slow_recognition_times_out(bot_env, monkeypatch):
    import bot.handlers.voice as voice_mod

    monkeypatch.setattr(voice_mod, "VOICE_RECOGNIZE_S", 0.05)
    dp, bot, session, deps = await _setup(bot_env, _SlowSpeech(["привет"], delay=5), [chat_json("Привет!")])
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_FAILED]
    assert deps.vision.completions == []
    assert await _ai_used(deps) == 0


async def test_router_budget_shrinks_by_recognition_time(bot_env, monkeypatch):
    import bot.handlers.voice as voice_mod
    from bot.handlers.meals import VISION_BUDGET_S

    seen: list[float] = []

    async def fake_handle_text(message, state, deps, text, *, prefix="", budget=None):
        seen.append(budget)

    monkeypatch.setattr(voice_mod, "handle_text", fake_handle_text)
    monkeypatch.setattr(voice_mod, "VOICE_TOTAL_S", 1.0)
    dp, bot, session, deps = await _setup(bot_env, _SlowSpeech(["привет"], delay=0.3))
    await dp.feed_update(bot, make_message(voice=5))
    assert len(seen) == 1 and 0 < seen[0] <= 0.71

    # обычное голосовое: срок маршрутизатора не больше, чем у текста
    seen.clear()
    monkeypatch.setattr(voice_mod, "VOICE_TOTAL_S", 110.0)
    deps.speech = FakeSpeech(["привет"])
    await dp.feed_update(bot, make_message(voice=5))
    assert seen == [VISION_BUDGET_S]


async def test_voice_prefix_on_weight_shortcut(bot_env):
    dp, bot, session, deps = await _setup(bot_env, FakeSpeech(["вес 85,5"]))
    await dp.feed_update(bot, make_message(voice=4))
    assert session.texts()[-1].startswith("🎙 Понял так: вес 85,5\n\n")


async def test_voice_prefix_on_profile_line(bot_env):
    dp, bot, session, deps = await _setup(bot_env, FakeSpeech(["жен 1996 165 70"]))
    await dp.feed_update(bot, make_message(voice=4))
    assert session.texts()[-1].startswith("🎙 Понял так: жен 1996 165 70\n\n")
    assert await get_fsm_state(dp) is not None
