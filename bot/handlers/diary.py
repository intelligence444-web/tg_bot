"""Дневник «📅 Сегодня» (спец. 6.5, 8.4)."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import keyboards, texts
from bot.calc import effective_norm
from bot.diary import format_day, summarize, to_local
from bot.handlers.onboarding import start_onboarding
from bot.progress import streak

if TYPE_CHECKING:
    from bot.app import Deps

STREAK_LOOKBACK = timedelta(days=60)


async def on_today(message: Message, state: FSMContext, deps: Deps) -> None:
    user_id = message.from_user.id
    user = await deps.storage.get_user(user_id)
    if user is None or user.profile is None or user.norm is None:
        await message.answer(texts.NEED_ONBOARDING)
        await start_onboarding(message, state)
        return
    today = to_local(deps.clock()).date()
    meals = await deps.storage.meals_on(user_id, today)
    days = await deps.storage.meal_days(user_id, today - STREAK_LOOKBACK)
    text = format_day(summarize(meals), effective_norm(user, today), streak(days, today))
    await message.answer(text, reply_markup=keyboards.main_menu())


def build_router() -> Router:
    r = Router(name="diary")
    r.message.register(on_today, F.text == texts.BTN_TODAY)
    return r
