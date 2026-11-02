"""Голосовые сообщения (v1.1): SpeechKit → текст → общий маршрутизатор текста.

Только вне состояний FSM. Распознавание единицу лимита ИИ не тратит — её тратит ``handle_text``,
если дойдёт до модели. Ни аудио, ни распознанный текст в лог не пишутся.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import texts
from bot.handlers.text import handle_text
from bot.speech import SpeechError

if TYPE_CHECKING:
    from bot.app import Deps

log = logging.getLogger(__name__)

MAX_VOICE_SECONDS = 30


async def on_voice(message: Message, state: FSMContext, deps: Deps) -> None:
    if await state.get_state() is not None:
        await message.answer(texts.VOICE_IN_STATE)
        return
    if message.voice.duration > MAX_VOICE_SECONDS:
        await message.answer(texts.VOICE_TOO_LONG)
        return
    if deps.speech is None:
        await message.answer(texts.VOICE_FAILED)
        return
    try:
        audio = await deps.fetch_photo(message.bot, message.voice.file_id)
        recognized = await deps.speech.recognize(audio)
    except (SpeechError, TelegramAPIError, OSError) as e:
        log.warning("voice failed", extra={"user_id": message.from_user.id, "error_kind": type(e).__name__})
        await message.answer(texts.VOICE_FAILED)
        return
    recognized = recognized.strip()
    if not recognized:
        await message.answer(texts.VOICE_EMPTY)
        return
    await handle_text(message, state, deps, recognized, prefix=f"🎙 Понял так: {recognized}")


async def on_video_note_or_audio(message: Message) -> None:
    await message.answer(texts.VOICE_ONLY)


def build_router() -> Router:
    r = Router(name="voice")
    r.message.register(on_voice, F.voice)
    r.message.register(on_video_note_or_audio, F.video_note | F.audio)
    return r
