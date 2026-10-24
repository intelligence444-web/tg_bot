"""Взвешивание, прогресс, график и поздравления (спец. 6.6–6.8, 8, 9.1, 9.2, 9.4, 13).

Колбэки (``bot.keyboards``): ``chart``, ``goal_done:{maintain|new}``, ``corridor:{back|keep}``,
``norm:recalc``. Ввод веса — состояние ``WeightInput.value``; любое другое сообщение в этом
состоянии (кнопка меню, фото, команда) сбрасывает его и обрабатывается как обычно.

Норма пересчитывается по весу тренда. Пока ``today <= maintain_transition_until``,
ккал = (``transition_from_kcal`` + TDEE) / 2 — плавный выход после достигнутой цели (9.2).
"""
from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.calc import calculate_norm, format_num, parse_number, planned_pace_kg_week, round_half_up
from bot.chart import render_weight_chart
from bot.diary import to_local
from bot.handlers.onboarding import WEIGHT_RANGE, start_goal_step, start_onboarding
from bot.models import User, Weighing
from bot.progress import (
    actual_pace,
    drift,
    forecast_date,
    new_milestones,
    passed_kg,
    progress_percent,
    trend_weight,
)

if TYPE_CHECKING:
    from bot.app import Deps

log = logging.getLogger(__name__)

CORRIDOR_KG = 1.5
TRANSITION_DAYS = 14
WEEK = timedelta(days=7)
_EPS = 1e-9
MENU_TEXTS = (texts.BTN_TODAY, texts.BTN_WEIGH, texts.BTN_PROFILE, texts.BTN_HELP)


class WeightInput(StatesGroup):
    value = State()


# --- расчёты ---


def today_for(deps: Deps) -> date:
    return to_local(deps.clock()).date()


def goal_target(user: User) -> float:
    """Желаемый вес N; у «держать вес» из анкеты его нет — тогда стартовый вес."""
    p = user.profile
    if p.target_weight_kg is not None:
        return p.target_weight_kg
    return user.start_weight_kg if user.start_weight_kg is not None else p.weight_kg


def current_trend(user: User, weighings: list[Weighing], today: date) -> float:
    trend = trend_weight(weighings, today)
    return user.profile.weight_kg if trend is None else trend


def _start(user: User) -> float:
    return user.start_weight_kg if user.start_weight_kg is not None else user.profile.weight_kg


def in_transition(user: User, today: date) -> bool:
    return (
        user.maintain_transition_until is not None
        and user.transition_from_kcal is not None
        and today <= user.maintain_transition_until
    )


def recalc_norm(user: User, trend: float, today: date) -> None:
    """Норма по весу тренда при прежних цели и активности (с плавным выходом, если он идёт)."""
    profile = replace(user.profile, weight_kg=trend)
    norm = calculate_norm(profile)
    if in_transition(user, today):
        norm = calculate_norm(profile, kcal_override=(user.transition_from_kcal + norm.tdee) / 2)
    user.profile = profile
    user.norm = norm


def corridor_out(user: User, trend: float) -> bool:
    return user.profile.goal == "maintain" and abs(trend - goal_target(user)) > CORRIDOR_KG + _EPS


def _pace(user: User, weighings: list[Weighing], today: date) -> float:
    pace = actual_pace(weighings, today)
    if pace is None:
        planned = planned_pace_kg_week(user.norm)
        pace = -planned if user.profile.goal == "lose" else planned
    return pace


def _forecast(user: User, weighings: list[Weighing], today: date) -> date | None:
    trend = current_trend(user, weighings, today)
    target = goal_target(user)
    if progress_percent(_start(user), trend, target) >= 100 - _EPS:
        return None
    return forecast_date(trend, target, _pace(user, weighings, today), today)


def format_date(d: date, today: date) -> str:
    month = texts.MONTHS_GEN[d.month - 1]
    if (d - today).days >= 365:
        return texts.FORECAST_DATE_YEAR.format(day=d.day, month=month, year=d.year)
    return texts.FORECAST_DATE.format(day=d.day, month=month)


def format_progress(user: User, weighings: list[Weighing], today: date) -> str:
    """Шкала прогресса (спец. 6.7) или коридор для цели «держать вес»."""
    trend = current_trend(user, weighings, today)
    target = goal_target(user)
    if user.profile.goal == "maintain":
        if corridor_out(user, trend):
            return texts.CORRIDOR_OUT.format(delta=format_num(abs(trend - target), 1), target=format_num(target, 1))
        return texts.CORRIDOR_IN.format(target=format_num(target, 1))
    start = _start(user)
    pct = progress_percent(start, trend, target)
    filled = max(0, min(10, math.floor(pct / 10 + _EPS)))
    lines = [
        texts.PROGRESS_HEAD.format(start=format_num(start, 1), target=format_num(target, 1)),
        texts.PROGRESS_PASSED.format(
            passed=format_num(passed_kg(start, trend, target), 1),
            total=format_num(abs(start - target), 1),
            bar="▓" * filled + "░" * (10 - filled),
            pct=format_num(pct),
        ),
    ]
    if pct >= 100 - _EPS:
        lines.append(texts.PROGRESS_DONE)
    else:
        pace = _pace(user, weighings, today)
        when = forecast_date(trend, target, pace, today)
        if when is None:
            lines.append(texts.PROGRESS_STANDS)
        else:
            lines.append(texts.PROGRESS_PACE.format(pace=format_num(abs(pace), 1), date=format_date(when, today)))
    return "\n".join(lines)


def week_change(weighings: list[Weighing], day: date) -> str | None:
    """«За неделю: −0,4 кг» — тренд сегодня против тренда 7 дней назад."""
    now = trend_weight(weighings, day)
    before = trend_weight(weighings, day - WEEK)
    if now is None or before is None:
        return None
    diff = now - before
    value = round_half_up(abs(diff), 1)
    sign = "" if value == 0 else ("−" if diff < 0 else "+")
    return texts.WEEK_CHANGE.format(delta=sign + format_num(value, 1))


async def _goal_text(user: User, trend: float, day: date, deps: Deps) -> str:
    started = user.goal_started_at or day
    weeks = (day - started).days // 7
    weeks_text = texts.LESS_THAN_WEEK if weeks == 0 else f"{weeks} {texts.plural(weeks, *texts.WEEK_WORDS)}"
    days = len(await deps.storage.meal_days(user.user_id, started))
    return texts.MILESTONES["goal"].format(kg=format_num(abs(_start(user) - trend), 1), weeks=weeks_text, days=days)


async def weighing_reply(user: User, day: date, kg: float, deps: Deps) -> tuple[str, InlineKeyboardMarkup]:
    """Сохраняет вес, пересчитывает норму и этапы; возвращает текст ответа и кнопки."""
    await deps.storage.upsert_weight(user.user_id, day, kg)
    weighings = await deps.storage.weights(user.user_id)
    trend = current_trend(user, weighings, day)
    recalc_norm(user, trend, day)
    head = format_progress(user, weighings, day)
    change = week_change(weighings, day)
    parts = [head if change is None else f"{head}\n{change}"]
    goal_done = recalc = False
    corridor_target: str | None = None
    goal = user.profile.goal
    if goal == "maintain":
        if corridor_out(user, trend):
            corridor_target = format_num(goal_target(user), 1)
        elif drift(weighings, day, goal_target(user), goal).weekly_up:
            parts.append(texts.WEIGHT_UP)
    else:
        target = goal_target(user)
        new = new_milestones(_start(user), trend, target, goal, user.milestones_sent)
        moved = drift(weighings, day, target, goal)
        if new:
            user.milestones_sent |= set(new)
            senior = new[-1]
            if senior == "goal":
                parts.append(await _goal_text(user, trend, day, deps))
                goal_done = True
            else:
                parts.append(texts.MILESTONES[senior])
        elif moved.weekly_up:
            parts.append(texts.WEIGHT_UP)
        if moved.three_weeks:
            parts.append(texts.NORM_FIX)
            recalc = True
    await deps.storage.save_user(user)
    markup = keyboards.weighing_result(goal_done=goal_done, corridor_target=corridor_target, recalc=recalc)
    return "\n\n".join(parts), markup


async def apply_weighing(user: User, day: date, kg: float, deps: Deps) -> str:
    """То же, что ``weighing_reply``, только текст ответа."""
    text, _ = await weighing_reply(user, day, kg, deps)
    return text


# --- общие куски обработчиков ---


async def load_profile_user(deps: Deps, user_id: int) -> User | None:
    user = await deps.storage.get_user(user_id)
    if user is None or user.profile is None or user.norm is None:
        return None
    return user


async def ask_onboarding(message: Message, state: FSMContext) -> None:
    await message.answer(texts.NEED_ONBOARDING)
    await start_onboarding(message, state)


async def trend_now(user: User, deps: Deps) -> float:
    weighings = await deps.storage.weights(user.user_id)
    return current_trend(user, weighings, today_for(deps))


# --- ввод веса ---


async def on_weigh(message: Message, state: FSMContext, deps: Deps) -> None:
    if await load_profile_user(deps, message.from_user.id) is None:
        await ask_onboarding(message, state)
        return
    await state.set_state(WeightInput.value)
    await state.set_data({})
    await message.answer(texts.ASK_WEIGHING)


async def on_weight_value(message: Message, state: FSMContext, deps: Deps) -> None:
    kg = parse_number(message.text or "", *WEIGHT_RANGE)
    if kg is None:
        await message.answer(texts.BAD_WEIGHT)
        return
    await state.clear()
    user = await load_profile_user(deps, message.from_user.id)
    if user is None:
        await ask_onboarding(message, state)
        return
    text, markup = await weighing_reply(user, today_for(deps), kg, deps)
    await message.answer(text, reply_markup=markup)


async def leave_weight_input(message: Message, state: FSMContext) -> None:
    """Не число, а другое действие: выходим из ввода веса и отдаём сообщение дальше."""
    await state.clear()
    raise SkipHandler


# --- колбэки ---


async def on_chart(callback: CallbackQuery, deps: Deps) -> None:
    await callback.answer()
    user = await load_profile_user(deps, callback.from_user.id)
    if user is None:
        return
    weighings = await deps.storage.weights(user.user_id)
    if len(weighings) < 2:
        await callback.message.answer(texts.CHART_LATER)
        return
    today = today_for(deps)
    target = goal_target(user)
    forecast = None
    if user.profile.goal != "maintain":
        when = _forecast(user, weighings, today)
        forecast = None if when is None else (when, target)
    try:
        png = render_weight_chart(weighings, target, forecast)
    except Exception:
        log.warning("chart render failed", extra={"user_id": user.user_id, "error_kind": "chart"})
        text = f"{texts.CHART_UNAVAILABLE}\n\n{format_progress(user, weighings, today)}"
        await callback.message.answer(text)
        return
    await callback.message.answer_photo(BufferedInputFile(png, filename="weight.png"))


async def on_goal_done(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    user = await load_profile_user(deps, callback.from_user.id)
    # Кнопки актуальны, только пока цель достигнута и не сменена: finish_onboarding
    # и «Держать вес» сбрасывают milestones_sent.
    if user is None or "goal" not in user.milestones_sent or user.profile.goal not in ("lose", "gain"):
        await callback.answer(texts.STALE_BUTTON)
        return
    await callback.answer()
    choice = callback.data.removeprefix(keyboards.CB_GOAL_DONE)
    trend = await trend_now(user, deps)
    if choice == "new":
        await start_goal_step(callback.message, state, user, weight_kg=trend)
        return
    if choice != "maintain":
        return
    today = today_for(deps)
    old_kcal = user.norm.kcal
    profile = replace(user.profile, goal="maintain", target_weight_kg=trend, weight_kg=trend)
    tdee = calculate_norm(profile).tdee
    until = today + timedelta(days=TRANSITION_DAYS)
    user.profile = profile
    user.norm = calculate_norm(profile, kcal_override=(old_kcal + tdee) / 2)
    user.maintain_transition_until = until
    user.transition_from_kcal = old_kcal
    user.start_weight_kg = trend
    user.goal_started_at = today
    user.milestones_sent = set()
    await deps.storage.save_user(user)
    await callback.message.answer(
        texts.MAINTAIN_START.format(
            target=format_num(trend, 1),
            kcal=format_num(user.norm.kcal),
            until=format_date(until + timedelta(days=1), today),
            tdee=format_num(tdee),
        ),
        reply_markup=keyboards.main_menu(),
    )


async def on_corridor(callback: CallbackQuery, deps: Deps) -> None:
    await callback.answer()
    user = await load_profile_user(deps, callback.from_user.id)
    if user is None or user.profile.goal != "maintain":
        return
    choice = callback.data.removeprefix(keyboards.CB_CORRIDOR)
    today = today_for(deps)
    weighings = await deps.storage.weights(user.user_id)
    trend = current_trend(user, weighings, today)
    target = goal_target(user)
    if choice == "back":
        if not corridor_out(user, trend):
            return
        goal = "lose" if trend > target else "gain"
        user.profile = replace(user.profile, goal=goal, target_weight_kg=target, weight_kg=trend)
        user.norm = calculate_norm(user.profile)
        user.start_weight_kg = trend
        user.goal_started_at = today
        user.milestones_sent = set()
        user.maintain_transition_until = None
        user.transition_from_kcal = None
        await deps.storage.save_user(user)
        text = texts.CORRIDOR_BACK_DONE.format(target=format_num(target, 1), kcal=format_num(user.norm.kcal))
        await callback.message.answer(f"{text}\n\n{format_progress(user, weighings, today)}")
        return
    if choice != "keep":
        return
    user.profile = replace(user.profile, target_weight_kg=trend)
    recalc_norm(user, trend, today)
    await deps.storage.save_user(user)
    await callback.message.answer(
        texts.CORRIDOR_KEEP_DONE.format(target=format_num(trend, 1), kcal=format_num(user.norm.kcal))
    )


async def on_norm_recalc(callback: CallbackQuery, deps: Deps) -> None:
    await callback.answer()
    user = await load_profile_user(deps, callback.from_user.id)
    if user is None:
        return
    trend = await trend_now(user, deps)
    recalc_norm(user, trend, today_for(deps))
    await deps.storage.save_user(user)
    await callback.message.answer(
        texts.NORM_RECALCED.format(weight=format_num(trend, 1), kcal=format_num(user.norm.kcal))
    )


def build_router() -> Router:
    r = Router(name="weight")
    r.message.register(
        on_weight_value,
        WeightInput.value,
        F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_TEXTS),
    )
    r.message.register(leave_weight_input, WeightInput.value)
    r.message.register(on_weigh, F.text == texts.BTN_WEIGH)
    r.callback_query.register(on_chart, F.data == keyboards.CB_CHART)
    r.callback_query.register(on_goal_done, F.data.startswith(keyboards.CB_GOAL_DONE))
    r.callback_query.register(on_corridor, F.data.startswith(keyboards.CB_CORRIDOR))
    r.callback_query.register(on_norm_recalc, F.data == keyboards.CB_NORM_RECALC)
    return r
