"""Активность (v1.1 9): шаги и тренировки, половина расхода — к норме дня.

Шаги распознаёт шаблон в ``bot.handlers.text`` (без ИИ и лимита), тренировки — kind ``activity``
маршрутизатора. Кнопка под ответом: ``actdel:{activity_id}`` (``bot.keyboards.CB_ACT_DEL``).
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.calc import activity_credit, effective_norm, format_num, steps_kcal, training_kcal
from bot.diary import to_local
from bot.models import Activity, User
from bot.progress import trend_weight

if TYPE_CHECKING:
    from bot.app import Deps

MAX_ACTIVITY_NAME = 60  # знаков в названии тренировки от модели


def _has_norm(user: User) -> bool:
    return user.profile is not None and user.norm is not None


async def day_credit(deps: Deps, user_id: int, day: date) -> float:
    return sum(a.kcal_credit for a in await deps.storage.activities_on(user_id, day))


async def day_norm_kcal(deps: Deps, user: User, day: date) -> float | None:
    """Норма дня в ккал с добавкой активности; без профиля — None."""
    if not _has_norm(user):
        return None
    return effective_norm(user, day).kcal + await day_credit(deps, user.user_id, day)


async def _weight(deps: Deps, user: User, day: date) -> float:
    """Вес для формул: тренд, если есть взвешивания, иначе вес из профиля."""
    trend = trend_weight(await deps.storage.weights(user.user_id), day)
    return trend if trend is not None else user.profile.weight_kg


async def _record(
    message: Message, deps: Deps, user: User, day: date, name: str, burned: float, head: str, prefix: str
) -> None:
    """Запись активности и ответ «<head> — ~N ккал, к норме сегодня +M ккал. Осталось: K ккал»."""
    credit = activity_credit(burned)
    act = Activity(
        user_id=user.user_id,
        activity_id=uuid4().hex,
        local_date=day,
        name=name,
        kcal_burned=burned,
        kcal_credit=credit,
        created_at=deps.clock(),
    )
    await deps.storage.add_activity(act)
    norm = await day_norm_kcal(deps, user, day)
    eaten = sum(m.analysis.kcal for m in await deps.storage.meals_on(user.user_id, day))
    text = texts.ACTIVITY_REPLY.format(
        head=head,
        kcal=format_num(burned), credit=format_num(credit), left=format_num(max(0.0, norm - eaten))
    )
    if prefix:
        text = f"{prefix}\n\n{text}"
    await message.answer(text, reply_markup=keyboards.activity_result(act.activity_id))


async def _profile_user(message: Message, deps: Deps, prefix: str) -> User | None:
    user = await deps.storage.get_user(message.from_user.id)
    if user is None or not _has_norm(user):
        await message.answer(f"{prefix}\n\n{texts.PROFILE_HINT}" if prefix else texts.PROFILE_HINT)
        return None
    return user


async def record_steps(message: Message, deps: Deps, steps: int, *, prefix: str = "") -> None:
    user = await _profile_user(message, deps, prefix)
    if user is None:
        return
    day = to_local(deps.clock()).date()
    burned = steps_kcal(steps, await _weight(deps, user, day))
    head = texts.STEPS_HEAD.format(steps=format_num(steps), word=texts.plural(steps, *texts.STEP_WORDS))
    await _record(message, deps, user, day, texts.STEPS_NAME, burned, head, prefix)


async def record_training(
    message: Message, deps: Deps, name: str, minutes: float, met: float, *, prefix: str = ""
) -> None:
    user = await _profile_user(message, deps, prefix)
    if user is None:
        return
    day = to_local(deps.clock()).date()
    burned = training_kcal(met, minutes, await _weight(deps, user, day))
    name = (name or texts.TRAINING_NAME)[:MAX_ACTIVITY_NAME].strip()
    head = texts.TRAINING_HEAD.format(name=name[:1].upper() + name[1:], minutes=format_num(minutes))
    await _record(message, deps, user, day, name, burned, head, prefix)


async def on_delete(callback: CallbackQuery, deps: Deps) -> None:
    activity_id = callback.data.removeprefix(keyboards.CB_ACT_DEL)
    if not activity_id or not await deps.storage.delete_activity(callback.from_user.id, activity_id):
        await callback.answer(texts.MEAL_DELETED)
        return
    await callback.answer()
    # Локальный импорт: meals → activity (day_norm_kcal), обратная ссылка только здесь.
    from bot.handlers.meals import _edit_or_send  # noqa: PLC0415

    await _edit_or_send(callback.message, texts.ACTIVITY_REMOVED, None)


def build_router() -> Router:
    r = Router(name="activity")
    r.callback_query.register(on_delete, F.data.startswith(keyboards.CB_ACT_DEL))
    return r
