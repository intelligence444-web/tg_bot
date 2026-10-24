"""«👤 Мой профиль» (спец. 6.9): данные, норма, шкала прогресса и кнопки.

Колбэки: ``profile:goal`` — шаги 6–7 анкеты со стартовым весом = вес тренда (как
``goal_done:new``); ``profile:restart`` — анкета заново (``onboarded_at`` и ``course_day``
не меняются, см. ``finish_onboarding``). ``chart`` обрабатывает ``bot.handlers.weight``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.calc import effective_norm, format_num
from bot.handlers.onboarding import start_goal_step, start_onboarding
from bot.handlers.weight import (
    ask_onboarding,
    corridor_out,
    current_trend,
    format_progress,
    goal_target,
    load_profile_user,
    today_for,
    trend_now,
)
from bot.models import User, Weighing

if TYPE_CHECKING:
    from datetime import date

    from bot.app import Deps


def format_profile(user: User, weighings: list[Weighing], today: date) -> str:
    p, n = user.profile, effective_norm(user, today)
    trend = current_trend(user, weighings, today)
    goal = texts.GOAL_LABELS[p.goal]
    if p.target_weight_kg is not None:
        goal = texts.PROFILE_GOAL_TARGET.format(goal=goal, target=format_num(p.target_weight_kg, 1))
    body = texts.PROFILE_BODY.format(
        sex=texts.SEX_LABELS[p.sex],
        age=p.age,
        age_word=texts.plural(p.age, *texts.YEAR_WORDS),
        height=format_num(p.height_cm),
        weight=format_num(trend, 1),
        activity=texts.ACTIVITY_LABELS[p.activity],
        goal=goal,
    )
    norm = texts.PROFILE_NORM.format(
        kcal=format_num(n.kcal),
        protein=format_num(n.protein_g),
        fat=format_num(n.fat_g),
        carbs=format_num(n.carbs_g),
        water=format_num(n.water_ml / 1000, 1),
    )
    return "\n\n".join([f"{texts.PROFILE_HEAD}\n{body}", norm, format_progress(user, weighings, today)])


async def on_profile(message: Message, state: FSMContext, deps: Deps) -> None:
    user = await load_profile_user(deps, message.from_user.id)
    if user is None:
        await ask_onboarding(message, state)
        return
    today = today_for(deps)
    weighings = await deps.storage.weights(user.user_id)
    trend = current_trend(user, weighings, today)
    corridor = format_num(goal_target(user), 1) if corridor_out(user, trend) else None
    await message.answer(format_profile(user, weighings, today), reply_markup=keyboards.profile(corridor))


async def on_profile_goal(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    await callback.answer()
    user = await load_profile_user(deps, callback.from_user.id)
    if user is None:
        await start_onboarding(callback.message, state)
        return
    await start_goal_step(callback.message, state, user, weight_kg=await trend_now(user, deps))


async def on_profile_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await start_onboarding(callback.message, state)


def build_router() -> Router:
    r = Router(name="profile")
    r.message.register(on_profile, F.text == texts.BTN_PROFILE)
    r.callback_query.register(on_profile_goal, F.data == keyboards.CB_PROFILE_GOAL)
    r.callback_query.register(on_profile_restart, F.data == keyboards.CB_PROFILE_RESTART)
    return r
