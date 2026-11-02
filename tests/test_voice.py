"""Голосовые сообщения (v1.1): SpeechKit → handle_text."""
from __future__ import annotations

from bot import texts
from bot.speech import SpeechError
from tests.fakes import FakeSpeech, FakeVision, fsm_context, make_message, onboard
from bot.handlers.meals import MealEdit
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
    assert session.texts() == [texts.VOICE_TOO_LONG]
    assert speech.calls == []
    assert await _ai_used(deps) == 0


async def test_empty_recognition(bot_env):
    dp, bot, session, deps = await _setup(bot_env, FakeSpeech(["  "]))
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_EMPTY]
    assert await _ai_used(deps) == 0


async def test_speech_error(bot_env):
    dp, bot, session, deps = await _setup(bot_env, FakeSpeech([SpeechError("boom")]))
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_FAILED]
    assert await _ai_used(deps) == 0


async def test_no_speech_configured(bot_env):
    dp, bot, session, deps = await _setup(bot_env, None)
    await dp.feed_update(bot, make_message(voice=5))
    assert session.texts() == [texts.VOICE_FAILED]


async def test_video_note_and_audio(bot_env):
    speech = FakeSpeech([])
    dp, bot, session, deps = await _setup(bot_env, speech)
    await dp.feed_update(bot, make_message(video_note=True))
    await dp.feed_update(bot, make_message(audio=True))
    assert session.texts() == [texts.VOICE_ONLY, texts.VOICE_ONLY]
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
