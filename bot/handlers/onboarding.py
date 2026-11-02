"""Профиль одной строкой (v1.1 3, 4) и расчёт нормы (спец. 6.3, 7).

Строка профиля («жен, 1996, 165, 70») разбирается ``bot.profile_line.parse_profile_line``;
``handle_profile_text`` вызывается для текста вне состояний FSM (до Task 8 — временным
обработчиком этого роутера). Дальше — состояния ``ProfileFlow``: подтверждение
(``pf:yes`` / ``pf:no``) → активность (``act:``) → цель (``goal:``) → желаемый вес
(число; ``bmi:retry`` / ``bmi:ok`` при ИМТ < 18,5) → итог.

Данные FSM: ``sex``, ``age``, ``height``, ``weight``, ``activity``, ``goal``;
``pending_target`` — желаемый вес, ждущий подтверждения после предупреждения об ИМТ.

Вход на шаг цели («Новая цель», «Изменить цель») — ``start_goal_step(message, state, user,
weight_kg=...)``: кладёт в данные FSM ``{"goal_only": True, sex, age, height, weight, activity}``
из ``user.profile`` (``weight`` заменяется на ``weight_kg``, например вес тренда) и ставит
``ProfileFlow.goal``. Флаг ``goal_only`` отличается одним: первое взвешивание не записывается
(вес тренда — не замер). Стартовый вес, дата начала цели и сброс ``milestones_sent`` — как
при первом расчёте (``finish_onboarding``). Пока итог не показан, профиль не меняется.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
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
from bot.profile_line import ParsedProfile, parse_profile_line

if TYPE_CHECKING:
    from datetime import date

    from bot.app import Deps

WEIGHT_RANGE = (30, 300)
MIN_AGE = 14
MENU_TEXTS = (texts.BTN_TODAY, texts.BTN_WEIGH, texts.BTN_PROFILE, texts.BTN_HELP)


class ProfileFlow(StatesGroup):
    confirm = State()
    activity = State()
    goal = State()
    target = State()


# --- строка профиля ---


def profile_example(today: date) -> str:
    return texts.PROFILE_EXAMPLE.format(max_year=today.year - MIN_AGE)


def _confirm_text(data: dict[str, Any]) -> str:
    age = data["age"]
    return texts.PROFILE_CONFIRM.format(
        sex=texts.SEX_NOUNS[data["sex"]],
        age=age,
        age_word=texts.plural(age, *texts.YEAR_WORDS),
        height=format_num(data["height"]),
        weight=format_num(data["weight"], 1),
    )


async def start_profile_flow(message: Message, state: FSMContext, parsed: ParsedProfile) -> None:
    """Показывает разобранную строку профиля и спрашивает «верно?» (прежние данные FSM сбрасываются)."""
    data = {"sex": parsed.sex, "age": parsed.age, "height": parsed.height_cm, "weight": parsed.weight_kg}
    await state.set_state(ProfileFlow.confirm)
    await state.set_data(data)
    await message.answer(_confirm_text(data), reply_markup=keyboards.profile_confirm())


async def handle_profile_text(message: Message, state: FSMContext, deps: Deps) -> bool:
    """Строка профиля → подтверждение; похожая, но неразборчивая → пример с диапазонами.

    ``True``, если текст был профилем или «partial» (ответ отправлен), иначе ``False``.
    """
    today = to_local(deps.clock()).date()
    parsed = parse_profile_line(message.text or "", today)
    if parsed is None:
        return False
    if parsed == "partial":
        await message.answer(profile_example(today))
        return True
    await start_profile_flow(message, state, parsed)
    return True


async def ask_profile_line(message: Message, state: FSMContext, text: str = texts.PROFILE_ASK) -> None:
    """Просит прислать строку профиля (шаги сбрасываются: строку ловит обработчик вне состояний)."""
    await state.clear()
    await message.answer(text)


async def start_goal_step(message: Message, state: FSMContext, user: User, weight_kg: float | None = None) -> None:
    """Запускает шаг цели (цель и желаемый вес) для пользователя с профилем."""
    p = user.profile
    if p is None:
        await state.clear()
        await message.answer(texts.PROFILE_HINT)
        return
    await state.set_state(ProfileFlow.goal)
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


async def on_profile_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ProfileFlow.activity)
    await callback.message.answer(texts.ASK_ACTIVITY, reply_markup=keyboards.activity())


async def on_profile_no(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_profile_line(callback.message, state, texts.PROFILE_RESEND)


async def on_activity(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    value = callback.data.removeprefix(keyboards.CB_ACT)
    if value not in texts.ACTIVITY_LABELS:
        return
    await state.update_data(activity=value)
    await state.set_state(ProfileFlow.goal)
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
    await state.set_state(ProfileFlow.target)
    await callback.message.answer(texts.ASK_TARGET)


async def _reask(message: Message, question: str, markup: InlineKeyboardMarkup) -> None:
    await message.answer(f"{texts.USE_BUTTONS}\n\n{question}", reply_markup=markup)


async def on_button_step_message(message: Message, state: FSMContext, deps: Deps) -> None:
    """Сообщение вместо кнопки: новая строка профиля начинает подтверждение заново, иначе — повтор вопроса."""
    if message.text and await handle_profile_text(message, state, deps):
        return
    current = await state.get_state()
    if current == ProfileFlow.confirm.state:
        await _reask(message, _confirm_text(await state.get_data()), keyboards.profile_confirm())
    elif current == ProfileFlow.activity.state:
        await _reask(message, texts.ASK_ACTIVITY, keyboards.activity())
    else:
        await _reask(message, texts.ASK_GOAL, keyboards.goal())


# --- желаемый вес ---


def _number(message: Message, bounds: tuple[float, float]) -> float | None:
    return parse_number(message.text or "", *bounds)


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


# --- выход из шагов профиля ---


def _is_escape(message: Message) -> bool:
    if message.text in MENU_TEXTS or message.photo:
        return True
    doc = message.document
    return doc is not None and (doc.mime_type or "").startswith("image/")


async def stay_in_profile_flow(message: Message, state: FSMContext, raw_state: str | None, deps: Deps) -> bool:
    """Фильтр роутера. У кого профиль уже есть (повторная строка, «Изменить цель»), кнопка меню
    или фото сбрасывают шаги, и сообщение уходит к следующим роутерам — как ``leave_weight_input``.

    Фильтр стоит на уровне роутера, а не обработчика: ``raw_state`` в данных апдейта не меняется
    после ``state.clear()``, и шаги ниже в том же роутере всё равно поймали бы сообщение.
    Первый расчёт (профиля ещё нет) так не покинуть: вопрос повторяется.
    """
    if raw_state not in ProfileFlow or not _is_escape(message):
        return True
    user = await deps.storage.get_user(message.from_user.id)
    if user is None or user.profile is None:
        return True
    await state.clear()
    return False


async def on_profile_text(message: Message, state: FSMContext, deps: Deps) -> None:
    """Временный обработчик текста вне состояний (Task 8 заменит его маршрутизатором текста)."""
    if not await handle_profile_text(message, state, deps):
        raise SkipHandler


def build_router() -> Router:
    r = Router(name="onboarding")
    r.message.filter(stay_in_profile_flow)
    r.callback_query.register(on_profile_yes, StateFilter(ProfileFlow.confirm), F.data == keyboards.CB_PROFILE_YES)
    r.callback_query.register(on_profile_no, StateFilter(ProfileFlow.confirm), F.data == keyboards.CB_PROFILE_NO)
    r.callback_query.register(on_activity, StateFilter(ProfileFlow.activity), F.data.startswith(keyboards.CB_ACT))
    r.callback_query.register(on_goal, StateFilter(ProfileFlow.goal), F.data.startswith(keyboards.CB_GOAL))
    r.callback_query.register(on_bmi_retry, StateFilter(ProfileFlow.target), F.data == keyboards.CB_BMI_RETRY)
    r.callback_query.register(on_bmi_ok, StateFilter(ProfileFlow.target), F.data == keyboards.CB_BMI_OK)
    # Любое сообщение на шаге с кнопками остаётся в шагах: строка профиля или повтор вопроса.
    r.message.register(
        on_button_step_message, StateFilter(ProfileFlow.confirm, ProfileFlow.activity, ProfileFlow.goal)
    )
    r.message.register(on_target, StateFilter(ProfileFlow.target))
    r.message.register(
        on_profile_text,
        StateFilter(None),
        F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_TEXTS),
    )
    return r
