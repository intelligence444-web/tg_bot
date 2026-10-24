"""/start, согласие (спец. 6.1) и «шлагбаум» до согласия."""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.handlers.onboarding import start_onboarding
from bot.models import User

if TYPE_CHECKING:
    from bot.app import Deps


async def _get_or_create(deps: Deps, user_id: int) -> User:
    user = await deps.storage.get_user(user_id)
    if user is None:
        user = User(user_id=user_id)
        await deps.storage.save_user(user)
    return user


async def on_start(message: Message, state: FSMContext, deps: Deps) -> None:
    user = await _get_or_create(deps, message.from_user.id)
    await state.clear()
    if user.consent_at is None:
        await message.answer(texts.WELCOME, reply_markup=keyboards.consent())
    elif user.profile is None:
        await start_onboarding(message, state)
    else:
        await message.answer(texts.WELCOME_BACK, reply_markup=keyboards.main_menu())


async def on_consent(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    await callback.answer()
    user = await _get_or_create(deps, callback.from_user.id)
    if user.consent_at is None:
        user.consent_at = deps.clock()
        await deps.storage.save_user(user)
    if user.profile is None:
        await start_onboarding(callback.message, state)
    else:
        await callback.message.answer(texts.WELCOME_BACK, reply_markup=keyboards.main_menu())


def build_router() -> Router:
    r = Router(name="start")
    r.message.register(on_start, CommandStart())
    r.callback_query.register(on_consent, F.data == keyboards.CB_CONSENT)
    return r


# --- до согласия ---


async def no_consent(event: Message | CallbackQuery, deps: Deps) -> bool:
    user = await deps.storage.get_user(event.from_user.id)
    return user is None or user.consent_at is None


async def ask_consent_message(message: Message) -> None:
    await message.answer(texts.CONSENT_REQUIRED, reply_markup=keyboards.consent())


async def ask_consent_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(texts.CONSENT_REQUIRED, reply_markup=keyboards.consent())


def build_consent_gate() -> Router:
    """Перехватывает всё, кроме /start и кнопки согласия, пока согласия нет."""
    r = Router(name="consent_gate")
    r.message.register(ask_consent_message, no_consent)
    r.callback_query.register(ask_consent_callback, no_consent)
    return r
