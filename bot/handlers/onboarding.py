"""Анкета (спец. 6.2–6.3, 7).

Данные шагов копятся в данных FSM: ``sex``, ``age``, ``height``, ``weight``, ``activity``,
``goal``, ``target``; ``pending_target`` — желаемый вес, ждущий подтверждения после
предупреждения об ИМТ.

Вход с шага 1 — ``start_onboarding(message, state)``.

Вход с шага 6 (новая цель, Task 10) — ``start_goal_step(message, state, user, weight_kg=...)``:
кладёт в данные FSM ``{"goal_only": True, sex, age, height, weight, activity}`` из
``user.profile`` (``weight`` заменяется на ``weight_kg``, например вес тренда) и ставит
состояние ``Onboarding.goal``. Дальше работают обычные обработчики шагов 6–7, а итог
собирается из тех же данных FSM. Флаг ``goal_only`` отличается одним: первое взвешивание
не записывается (вес тренда — не замер). Стартовый вес, дата начала цели и сброс
``milestones_sent`` — как при обычной анкете (``finish_onboarding``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.calc import (
    bmi,
    calculate_norm,
    format_num,
    lose_allowed,
    parse_number,
    planned_pace_kg_week,
    planned_weeks_to_goal,
    round_half_up,
    target_bmi_low,
)
from bot.diary import to_local
from bot.models import Norm, Profile, User

if TYPE_CHECKING:
    from bot.app import Deps

AGE_RANGE = (14, 100)
HEIGHT_RANGE = (120, 230)
WEIGHT_RANGE = (30, 300)


class Onboarding(StatesGroup):
    sex = State()
    age = State()
    height = State()
    weight = State()
    activity = State()
    goal = State()
    target = State()


# --- входы в сценарий ---


async def start_onboarding(message: Message, state: FSMContext) -> None:
    """Запускает анкету с шага 1 в чате ``message`` (данные прежней анкеты в FSM сбрасываются)."""
    await state.set_state(Onboarding.sex)
    await state.set_data({})
    await message.answer(texts.ASK_SEX, reply_markup=keyboards.sex())


async def start_goal_step(message: Message, state: FSMContext, user: User, weight_kg: float | None = None) -> None:
    """Запускает анкету с шага 6 (только цель и желаемый вес) для пользователя с профилем."""
    p = user.profile
    if p is None:
        await start_onboarding(message, state)
        return
    await state.set_state(Onboarding.goal)
    await state.set_data(
        {
            "goal_only": True,
            "sex": p.sex,
            "age": p.age,
            "height": p.height_cm,
            "weight": p.weight_kg if weight_kg is None else weight_kg,
            "activity": p.activity,
        }
    )
    await message.answer(texts.ASK_GOAL, reply_markup=keyboards.goal())


# --- итог ---


def format_summary(profile: Profile, norm: Norm) -> str:
    parts = [
        texts.SUMMARY_HEAD.format(bmr=format_num(norm.bmr), tdee=format_num(norm.tdee), kcal=format_num(norm.kcal)),
        texts.SUMMARY_MACROS.format(
            protein=format_num(norm.protein_g),
            fat=format_num(norm.fat_g),
            carbs=format_num(norm.carbs_g),
            water=format_num(norm.water_ml / 1000, 1),
        ),
    ]
    weeks = planned_weeks_to_goal(profile, norm)
    if weeks is None or profile.target_weight_kg is None:
        parts.append(texts.SUMMARY_MAINTAIN)
    else:
        n = int(round_half_up(weeks))
        parts.append(
            texts.SUMMARY_PACE.format(
                pace=format_num(planned_pace_kg_week(norm), 1),
                target=format_num(profile.target_weight_kg, 1),
                weeks=format_num(weeks),
                weeks_word=texts.plural(n, *texts.WEEK_WORDS),
            )
        )
    parts.append(texts.SUMMARY_TAIL)
    return "\n\n".join(parts)


async def finish_onboarding(user: User, profile: Profile, deps: Deps, *, record_weighing: bool = True) -> str:
    """Сохраняет профиль и норму, начинает цель заново; возвращает текст итога (спец. 6.3).

    ``start_weight_kg`` = вес профиля, ``goal_started_at`` = сегодня (МСК), ``onboarded_at`` —
    только если пусто, ``milestones_sent``, ``maintain_transition_until`` и ``transition_from_kcal`` сбрасываются,
    ``course_day`` не меняется. При ``record_weighing`` вес записывается взвешиванием за сегодня.
    """
    today = to_local(deps.clock()).date()
    norm = calculate_norm(profile)
    user.profile = profile
    user.norm = norm
    user.start_weight_kg = profile.weight_kg
    user.goal_started_at = today
    if user.onboarded_at is None:
        user.onboarded_at = today
    user.milestones_sent = set()
    user.maintain_transition_until = None
    user.transition_from_kcal = None
    if record_weighing:
        await deps.storage.upsert_weight(user.user_id, today, profile.weight_kg)
    await deps.storage.save_user(user)
    return format_summary(profile, norm)


async def _complete(message: Message, state: FSMContext, deps: Deps, user_id: int, target: float | None) -> None:
    data = await state.get_data()
    profile = Profile(
        sex=data["sex"],
        age=data["age"],
        height_cm=data["height"],
        weight_kg=data["weight"],
        activity=data["activity"],
        goal=data["goal"],
        target_weight_kg=target,
    )
    user = await deps.storage.get_user(user_id) or User(user_id=user_id)
    text = await finish_onboarding(user, profile, deps, record_weighing=not data.get("goal_only"))
    await state.clear()
    await message.answer(text, reply_markup=keyboards.main_menu())


# --- шаги с кнопками ---


async def _reask(message: Message, question: str, markup: InlineKeyboardMarkup) -> None:
    await message.answer(f"{texts.USE_BUTTONS}\n\n{question}", reply_markup=markup)


async def on_sex(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    value = callback.data.removeprefix(keyboards.CB_SEX)
    if value not in ("male", "female"):
        return
    await state.update_data(sex=value)
    await state.set_state(Onboarding.age)
    await callback.message.answer(texts.ASK_AGE)


async def on_activity(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    value = callback.data.removeprefix(keyboards.CB_ACT)
    if value not in texts.ACTIVITY_LABELS:
        return
    await state.update_data(activity=value)
    await state.set_state(Onboarding.goal)
    await callback.message.answer(texts.ASK_GOAL, reply_markup=keyboards.goal())


async def on_goal(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    await callback.answer()
    goal = callback.data.removeprefix(keyboards.CB_GOAL)
    if goal not in texts.GOAL_LABELS:
        return
    data = await state.get_data()
    if goal == "lose" and not lose_allowed(data["age"]):
        await callback.message.answer(texts.TEEN_MAINTAIN)
        goal = "maintain"
    await state.update_data(goal=goal)
    if goal == "maintain":
        await _complete(callback.message, state, deps, callback.from_user.id, None)
        return
    await state.set_state(Onboarding.target)
    await callback.message.answer(texts.ASK_TARGET)


async def reask_sex(message: Message) -> None:
    await _reask(message, texts.ASK_SEX, keyboards.sex())


async def reask_activity(message: Message) -> None:
    await _reask(message, texts.ASK_ACTIVITY, keyboards.activity())


async def reask_goal(message: Message) -> None:
    await _reask(message, texts.ASK_GOAL, keyboards.goal())


# --- шаги с числами ---


def _number(message: Message, bounds: tuple[float, float]) -> float | None:
    return parse_number(message.text or "", *bounds)


async def on_age(message: Message, state: FSMContext) -> None:
    value = _number(message, AGE_RANGE)
    if value is None or value != int(value):
        await message.answer(texts.BAD_AGE)
        return
    await state.update_data(age=int(value))
    await state.set_state(Onboarding.height)
    await message.answer(texts.ASK_HEIGHT)


async def on_height(message: Message, state: FSMContext) -> None:
    value = _number(message, HEIGHT_RANGE)
    if value is None:
        await message.answer(texts.BAD_HEIGHT)
        return
    await state.update_data(height=value)
    await state.set_state(Onboarding.weight)
    await message.answer(texts.ASK_WEIGHT)


async def on_weight(message: Message, state: FSMContext) -> None:
    value = _number(message, WEIGHT_RANGE)
    if value is None:
        await message.answer(texts.BAD_WEIGHT)
        return
    await state.update_data(weight=value)
    await state.set_state(Onboarding.activity)
    await message.answer(texts.ASK_ACTIVITY, reply_markup=keyboards.activity())


async def on_target(message: Message, state: FSMContext, deps: Deps) -> None:
    value = _number(message, WEIGHT_RANGE)
    if value is None:
        await message.answer(texts.BAD_TARGET)
        return
    data: dict[str, Any] = await state.get_data()
    weight = data["weight"]
    if data["goal"] == "lose" and value >= weight:
        await message.answer(texts.TARGET_LESS.format(weight=format_num(weight, 1)))
        return
    if data["goal"] == "gain" and value <= weight:
        await message.answer(texts.TARGET_MORE.format(weight=format_num(weight, 1)))
        return
    if target_bmi_low(value, data["height"]):
        await state.update_data(pending_target=value)
        await message.answer(
            texts.BMI_LOW.format(target=format_num(value, 1), bmi=format_num(bmi(value, data["height"]), 1)),
            reply_markup=keyboards.bmi_low(),
        )
        return
    await _complete(message, state, deps, message.from_user.id, value)


async def on_bmi_retry(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(pending_target=None)
    await callback.message.answer(texts.ASK_TARGET)


async def on_bmi_ok(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    await callback.answer()
    target = (await state.get_data()).get("pending_target")
    if target is None:
        await callback.message.answer(texts.ASK_TARGET)
        return
    await _complete(callback.message, state, deps, callback.from_user.id, target)


def build_router() -> Router:
    r = Router(name="onboarding")
    r.callback_query.register(on_sex, StateFilter(Onboarding.sex), F.data.startswith(keyboards.CB_SEX))
    r.callback_query.register(on_activity, StateFilter(Onboarding.activity), F.data.startswith(keyboards.CB_ACT))
    r.callback_query.register(on_goal, StateFilter(Onboarding.goal), F.data.startswith(keyboards.CB_GOAL))
    r.callback_query.register(on_bmi_retry, StateFilter(Onboarding.target), F.data == keyboards.CB_BMI_RETRY)
    r.callback_query.register(on_bmi_ok, StateFilter(Onboarding.target), F.data == keyboards.CB_BMI_OK)
    # Любое сообщение на шаге анкеты остаётся в анкете: на шаге с кнопками — повтор вопроса.
    r.message.register(reask_sex, StateFilter(Onboarding.sex))
    r.message.register(on_age, StateFilter(Onboarding.age))
    r.message.register(on_height, StateFilter(Onboarding.height))
    r.message.register(on_weight, StateFilter(Onboarding.weight))
    r.message.register(reask_activity, StateFilter(Onboarding.activity))
    r.message.register(reask_goal, StateFilter(Onboarding.goal))
    r.message.register(on_target, StateFilter(Onboarding.target))
    return r
