"""Голосовые сообщения (v1.1): SpeechKit → текст → общий маршрутизатор текста.

Только вне состояний FSM. Распознавание единицу лимита ИИ не тратит — её тратит ``handle_text``,
если дойдёт до модели. Скачивание и распознавание — до ``VOICE_RECOGNIZE_S``; модели остаётся
не больше ``VOICE_TOTAL_S`` минус уже прошедшее время. Ни аудио, ни распознанный текст в лог не пишутся.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import texts
from bot.handlers.meals import VISION_BUDGET_S
from bot.handlers.text import handle_text
from bot.speech import SpeechError

if TYPE_CHECKING:
    from bot.app import Deps

log = logging.getLogger(__name__)

MAX_VOICE_SECONDS = 30
VOICE_RECOGNIZE_S = 25.0  # срок на скачивание и распознавание
# Общий срок голосового до ответа (функция живёт 120 с); модели достаётся остаток, но не больше,
# чем тексту (VISION_BUDGET_S).
VOICE_TOTAL_S = 110.0


async def on_voice(message: Message, state: FSMContext, deps: Deps) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
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
        async with asyncio.timeout(VOICE_RECOGNIZE_S):
            audio = await deps.fetch_photo(message.bot, message.voice.file_id)
            recognized = await deps.speech.recognize(audio)
    except (SpeechError, TelegramAPIError, OSError, TimeoutError) as e:
        log.warning("voice failed", extra={"user_id": message.from_user.id, "error_kind": type(e).__name__})
        await message.answer(texts.VOICE_FAILED)
        return
    recognized = recognized.strip()
    if not recognized:
        await message.answer(texts.VOICE_EMPTY)
        return
    budget = min(VISION_BUDGET_S, VOICE_TOTAL_S - (loop.time() - started))
    await handle_text(message, state, deps, recognized, prefix=f"🎙 Понял так: {recognized}", budget=budget)


async def on_video_note_or_audio(message: Message) -> None:
    await message.answer(texts.VOICE_ONLY)


def build_router() -> Router:
    r = Router(name="voice")
    r.message.register(on_voice, F.voice)
    r.message.register(on_video_note_or_audio, F.video_note | F.audio)
    return r
