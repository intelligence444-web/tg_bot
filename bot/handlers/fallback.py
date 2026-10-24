"""Всё, что не подошло другим роутерам (спец. 13). Подключается последним."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts


async def on_any_message(message: Message) -> None:
    await message.answer(texts.FALLBACK, reply_markup=keyboards.main_menu())


async def on_stale_callback(callback: CallbackQuery) -> None:
    # Кнопка из старого сообщения (например, шаг анкеты, которая уже пройдена).
    await callback.answer()


def build_router() -> Router:
    r = Router(name="fallback")
    r.message.register(on_any_message)
    r.callback_query.register(on_stale_callback)
    return r
