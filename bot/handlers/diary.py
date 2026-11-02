"""Дневник «📅 Сегодня» (спец. 6.5, 8.4)."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import Message

from bot import keyboards, texts
from bot.calc import effective_norm
from bot.diary import format_day, format_eaten, summarize, to_local
from bot.handlers.activity import day_credit
from bot.progress import streak

if TYPE_CHECKING:
    from bot.app import Deps

STREAK_LOOKBACK = timedelta(days=60)


async def on_today(message: Message, deps: Deps) -> None:
    user_id = message.from_user.id
    user = await deps.storage.get_user(user_id)
    today = to_local(deps.clock()).date()
    meals = await deps.storage.meals_on(user_id, today)
    if user is None or user.profile is None or user.norm is None:
        # Без профиля (v1.1 3): съеденное без нормы и приглашение заполнить профиль.
        text = f"{format_eaten(summarize(meals))}\n\n{texts.PROFILE_HINT}"
        await message.answer(text, reply_markup=keyboards.main_menu())
        return
    days = await deps.storage.meal_days(user_id, today - STREAK_LOOKBACK)
    credit = await day_credit(deps, user_id, today)
    text = format_day(summarize(meals), effective_norm(user, today), streak(days, today), credit)
    await message.answer(text, reply_markup=keyboards.main_menu())


def build_router() -> Router:
    r = Router(name="diary")
    r.message.register(on_today, F.text == texts.BTN_TODAY)
    return r
