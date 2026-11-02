"""Фото еды, уточнение масла и правки записей (спец. 6.4, 8.5, 10, 12, 13).

Колбэки (``bot.keyboards``): ``fat:{meal_id}:{none|little|much|sauce}``, ``meal:edit:{meal_id}``,
``meal:del:{meal_id}``, ``meal:type:{meal_id}``, ``mt:{meal_id}:{breakfast|lunch|snack|dinner}``;
кнопку темы ``topic:{n}`` (v1.1 8) ставит ``record_meal``, нажатие обрабатывает ``bot.handlers.text``.
Правка текстом — состояния ``MealEdit.correction`` / ``MealEdit.sauce``, в данных ``meal_id``.
Фото нигде не сохраняется; в лог не пишутся ни байты, ни ответ модели.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar, get_args
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.calc import format_num
from bot.diary import meal_type_for, overeat_now, to_local
from bot.handlers.activity import day_norm_kcal
from bot.limits import take_ai
from bot.models import Analysis, Meal, MealType, User
from bot.vision import BadImage, VisionError, VisionUnavailable, prepare_image

if TYPE_CHECKING:
    from bot.app import Deps

log = logging.getLogger(__name__)
T = TypeVar("T")

FAT_GRAMS = {"none": 0.0, "little": 10.0, "much": 20.0}
MEAL_TYPES: tuple[MealType, ...] = get_args(MealType)
ALERT_INTERVAL = timedelta(hours=1)
TOPIC_HOUR = 18  # тему предлагаем под первой едой с этого часа (МСК)
SAUCE_PREFIX = "Соус: "
VISION_BUDGET_S = 95.0  # общий срок на распознавание вместе с запасной моделью и повторами
MAX_SHOWN_ITEMS = 15
MAX_NAME_LEN = 80


class MealEdit(StatesGroup):
    correction = State()
    sauce = State()


# --- форматирование ---


def format_meal(
    analysis: Analysis, meal_type: MealType, remaining: float | None, note: str | None, *, hint: str | None = None
) -> str:
    """Ответ на приём пищи. ``remaining=None`` — профиля нет: вместо строки остатка ``hint`` (или ничего)."""
    lines = []
    for i in analysis.items[:MAX_SHOWN_ITEMS]:
        name = i.name if len(i.name) <= MAX_NAME_LEN else i.name[: MAX_NAME_LEN - 1] + "…"
        line = texts.MEAL_ITEM.format(name=name, grams=format_num(i.grams), kcal=format_num(i.kcal))
        if i.hidden:
            line += texts.MEAL_ITEM_HIDDEN
        lines.append(line)
    hidden_count = len(analysis.items) - MAX_SHOWN_ITEMS
    if hidden_count > 0:
        lines.append(texts.MEAL_MORE_ITEMS.format(n=hidden_count, word=texts.plural(hidden_count, *texts.ITEM_WORDS)))
    lines.append(
        texts.MEAL_TOTAL.format(
            kcal=format_num(analysis.kcal),
            protein=format_num(analysis.protein),
            fat=format_num(analysis.fat),
            carbs=format_num(analysis.carbs),
        )
    )
    if analysis.confidence == "low":
        lines.append(texts.MEAL_LOW_CONFIDENCE)
    lines.append("")
    lines.append(
        texts.MEAL_SAVED.format(meal_type=texts.MEAL_TYPE_NAMES[meal_type], kcal=format_num(analysis.kcal))
    )
    if remaining is not None:
        lines.append(texts.MEAL_LEFT.format(left=format_num(max(0.0, remaining))))
    elif hint is not None:
        lines.append(hint)
    if note is not None:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _render(
    meal: Meal,
    remaining: float | None,
    note: str | None = None,
    *,
    hint: str | None = None,
    topic: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    text = format_meal(meal.analysis, meal.meal_type, remaining, note, hint=hint)
    ask = meal.analysis.ask_about_fat
    if ask:
        text += f"\n\n{texts.ASK_FAT}"
    return text, keyboards.meal_result(meal.meal_id, ask_fat=ask, topic=topic)


def _note(meal: Meal) -> str | None:
    """Строка под записью при перерисовке: комментарий к тарелке (сообщение о переборе не повторяем)."""
    return meal.analysis.comment or None


def _shown_topic(callback: CallbackQuery) -> int | None:
    """Тема с кнопки «💡» под сообщением, которое правится: при перерисовке кнопка остаётся."""
    msg = callback.message
    return keyboards.topic_of(msg.reply_markup) if isinstance(msg, Message) else None


async def _offer_topic(deps: Deps, user: User, now: datetime, earlier: list[Meal]) -> int | None:
    """Номер темы для кнопки под этой едой (v1.1 8) или None.

    Условия: советы включены, сейчас ≥ 18:00 МСК, это первая еда сегодня с 18:00 (``earlier`` —
    еда сегодня до этой), тема сегодня ещё не предлагалась, сегодня не было вопросов к ИИ.
    """
    local = to_local(now)
    today = local.date()
    if not user.tips_enabled or local.hour < TOPIC_HOUR or user.last_topic_offer_date == today:
        return None
    if any(to_local(m.eaten_at).hour >= TOPIC_HOUR for m in earlier):
        return None
    history = await deps.storage.recent_chat(user.user_id, now)
    if any(m.role == "user" and to_local(m.created_at).date() == today for m in history):
        return None
    return user.next_topic


async def _remaining(deps: Deps, user: User, day: date) -> float | None:
    """Остаток нормы дня (с добавкой активности); без профиля — None."""
    norm = await day_norm_kcal(deps, user, day)
    if norm is None:
        return None
    meals = await deps.storage.meals_on(user.user_id, day)
    return norm - sum(m.analysis.kcal for m in meals)


def _is_food(a: Analysis) -> bool:
    return a.is_food and bool(a.items)


async def _edit_or_send(
    target: Message | InaccessibleMessage, text: str, markup: InlineKeyboardMarkup | None
) -> None:
    """Правит сообщение бота; старое (недоступное) или нередактируемое — шлёт новым."""
    if not isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
        return
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await target.answer(text, reply_markup=markup)


async def _vision_call(call: Awaitable[T]) -> T:
    """Распознавание в пределах ``VISION_BUDGET_S``; не уложились — как «сервис недоступен»."""
    try:
        async with asyncio.timeout(VISION_BUDGET_S):
            return await call
    except TimeoutError as e:
        raise VisionUnavailable("vision time budget exceeded", no_credits=False) from e


async def _alert_no_credits(bot: Bot, deps: Deps) -> None:
    if deps.admin_chat_id is None:
        return
    if await deps.storage.alert_allowed("kie_no_credits", deps.clock(), ALERT_INTERVAL):
        try:
            await bot.send_message(deps.admin_chat_id, texts.ADMIN_KIE_NO_CREDITS)
        except TelegramAPIError:
            log.error("admin alert failed", exc_info=True, extra={"user_id": None, "error_kind": "admin_alert"})


# --- фото ---


async def on_photo(message: Message, bot: Bot, state: FSMContext, deps: Deps) -> None:
    user_id = message.from_user.id
    now = deps.clock()
    # Альбом отсекаем первым: остальные фото альбома не распознаются и не пишутся.
    group = message.media_group_id
    if group is not None and not await deps.storage.mark_processed(f"album:{group}", now):
        if await deps.storage.mark_processed(f"album-hint:{group}", now):
            await message.answer(texts.ALBUM_HINT)
        return

    if await state.get_state() in (MealEdit.correction.state, MealEdit.sauce.state):
        await state.clear()

    status = await message.answer(texts.ANALYZING)
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    try:
        raw = await deps.fetch_photo(bot, file_id)
    except Exception:  # сеть или Telegram: файл не скачался
        log.error("photo download failed", exc_info=True, extra={"user_id": user_id, "error_kind": "download"})
        await _edit_or_send(status, texts.OVERLOADED, None)
        return
    try:
        jpeg = prepare_image(raw)
    except BadImage:
        log.warning("photo not an image", extra={"user_id": user_id, "error_kind": "bad_image"})
        await _edit_or_send(status, texts.NOT_FOOD, None)
        return
    # Единица лимита — только когда фото скачано и это картинка, до вызова модели;
    # остальные фото альбома отсечены выше.
    if not await take_ai(deps.storage, user_id, to_local(now).date(), deps.ai_limit):
        log.info("ai limit reached", extra={"user_id": user_id, "error_kind": "ai_limit"})
        await _edit_or_send(status, texts.AI_LIMIT, None)
        return
    try:
        analysis = await _vision_call(deps.vision.analyze(jpeg, message.caption))
    except VisionError as e:
        no_credits = isinstance(e, VisionUnavailable) and e.no_credits
        log.warning(
            "vision unavailable",
            extra={"user_id": user_id, "error_kind": "no_credits" if no_credits else "vision_unavailable"},
        )
        await _edit_or_send(status, texts.OVERLOADED, None)
        if no_credits:
            await _alert_no_credits(bot, deps)
        return
    if not _is_food(analysis):
        await _edit_or_send(status, texts.NOT_FOOD, None)
        return
    # Распознавание идёт до полутора минут: за это время пользователь мог измениться
    # (/stop_tips), поэтому пишем поверх свежей записи, а не старой.
    user = await deps.storage.get_user(user_id) or User(user_id=user_id)
    await record_meal(message, deps, user, analysis, status_msg=status)


async def record_meal(
    message: Message, deps: Deps, user: User, analysis: Analysis, *, status_msg: Message | None, prefix: str = ""
) -> None:
    """Общий путь после распознавания (фото, текст, голос): запись, перебор, комментарий,
    подсказка профиля и кнопки. ``user`` — свежая запись, прочитанная после вызова модели.

    ``status_msg`` — сообщение «Анализирую…», которое заменяется ответом (None — ответ новым
    сообщением). ``prefix`` — простой текст первой строкой ответа (голос: «🎙 Понял так: …»).
    """
    user_id = user.user_id
    now = deps.clock()
    local = to_local(now)
    today = local.date()
    meal_type = meal_type_for(local)
    has_norm = user.profile is not None and user.norm is not None
    overeat = False
    before = await deps.storage.meals_on(user_id, today)
    topic = await _offer_topic(deps, user, now, before)
    if has_norm:
        day_before = sum(m.analysis.kcal for m in before)
        norm_kcal = await day_norm_kcal(deps, user, today)
        overeat = overeat_now(day_before, analysis.kcal, norm_kcal, user.last_overeat_date == today)
    meal = Meal(
        user_id=user_id,
        meal_id=uuid4().hex,
        eaten_at=now,
        local_date=today,
        meal_type=meal_type,
        analysis=analysis,
    )
    await deps.storage.add_meal(meal)
    # Комментарий к тарелке (v1.1 5.3); при переборе вместо него — сообщение о переборе.
    note = texts.OVEREAT if overeat else (analysis.comment or None)
    if topic is not None:
        user.last_topic_offer_date = today
        await deps.storage.save_user(user)
    if not has_norm:
        # До профиля вместо остатка — приглашение заполнить профиль, не чаще раза в день (v1.1 3, п. 2).
        hint = None
        if user.last_profile_hint_date != today:
            hint = texts.PROFILE_HINT
            user.last_profile_hint_date = today
            await deps.storage.save_user(user)
        text, markup = _render(meal, None, note, hint=hint, topic=topic)
    else:
        if overeat:
            user.last_overeat_date = today
            await deps.storage.save_user(user)
        text, markup = _render(meal, norm_kcal - day_before - analysis.kcal, note, topic=topic)
    if prefix:
        text = f"{prefix}\n\n{text}"
    if status_msg is None:
        await message.answer(text, reply_markup=markup)
    else:
        await _edit_or_send(status_msg, text, markup)


# --- кнопки под результатом ---


async def _load(callback: CallbackQuery, deps: Deps, meal_id: str) -> tuple[User, Meal] | None:
    meal = await deps.storage.get_meal(callback.from_user.id, meal_id)
    user = await deps.storage.get_user(callback.from_user.id)
    if meal is None or user is None:
        await callback.answer(texts.MEAL_DELETED)
        return None
    return user, meal


def _parts(data: str) -> tuple[str, str] | None:
    """``prefix:meal_id:value`` → (meal_id, value); иначе None."""
    parts = data.split(":")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


async def on_fat(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    parsed = _parts(callback.data)
    if parsed is None:
        await callback.answer()
        return
    meal_id, choice = parsed
    loaded = await _load(callback, deps, meal_id)
    if loaded is None:
        return
    user, meal = loaded
    if choice == "sauce":
        await callback.answer()
        await state.set_state(MealEdit.sauce)
        await state.set_data({"meal_id": meal_id})
        await callback.message.answer(texts.ASK_SAUCE)
        return
    grams = FAT_GRAMS.get(choice)
    if grams is None:
        await callback.answer()
        return
    meal.analysis = meal.analysis.with_fat(grams)
    meal.fat_answer = choice
    await deps.storage.update_meal(meal)
    await callback.answer()
    remaining = await _remaining(deps, user, meal.local_date)
    text, markup = _render(meal, remaining, _note(meal), topic=_shown_topic(callback))
    await _edit_or_send(callback.message, text, markup)


async def on_edit(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    meal_id = callback.data.removeprefix(keyboards.CB_MEAL_EDIT)
    if await _load(callback, deps, meal_id) is None:
        return
    await callback.answer()
    await state.set_state(MealEdit.correction)
    await state.set_data({"meal_id": meal_id})
    await callback.message.answer(texts.ASK_CORRECTION)


async def on_delete(callback: CallbackQuery, deps: Deps) -> None:
    meal_id = callback.data.removeprefix(keyboards.CB_MEAL_DEL)
    if not await deps.storage.delete_meal(callback.from_user.id, meal_id):
        await callback.answer(texts.MEAL_DELETED)
        return
    await callback.answer()
    await _edit_or_send(callback.message, texts.MEAL_REMOVED, None)


async def on_type_menu(callback: CallbackQuery, deps: Deps) -> None:
    meal_id = callback.data.removeprefix(keyboards.CB_MEAL_TYPE)
    if await _load(callback, deps, meal_id) is None:
        return
    await callback.answer(texts.ASK_MEAL_TYPE)
    markup = keyboards.meal_types(meal_id, topic=_shown_topic(callback))
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
    await callback.message.answer(texts.ASK_MEAL_TYPE, reply_markup=markup)


async def on_type_chosen(callback: CallbackQuery, deps: Deps) -> None:
    parsed = _parts(callback.data)
    if parsed is None:
        await callback.answer()
        return
    meal_id, meal_type = parsed
    loaded = await _load(callback, deps, meal_id)
    if loaded is None:
        return
    user, meal = loaded
    if meal_type not in MEAL_TYPES:
        await callback.answer()
        return
    meal.meal_type = meal_type
    await deps.storage.update_meal(meal)
    await callback.answer()
    remaining = await _remaining(deps, user, meal.local_date)
    text, markup = _render(meal, remaining, _note(meal), topic=_shown_topic(callback))
    await _edit_or_send(callback.message, text, markup)


# --- правка текстом ---


async def on_correction_text(message: Message, state: FSMContext, deps: Deps) -> None:
    current = await state.get_state()
    data = await state.get_data()
    await state.clear()
    user_id = message.from_user.id
    meal = await deps.storage.get_meal(user_id, data.get("meal_id", ""))
    user = await deps.storage.get_user(user_id)
    if meal is None or user is None:
        await message.answer(texts.MEAL_DELETED)
        return
    text = message.text
    if current == MealEdit.sauce.state:
        text = SAUCE_PREFIX + text
    if not await take_ai(deps.storage, user_id, to_local(deps.clock()).date(), deps.ai_limit):
        log.info("ai limit reached", extra={"user_id": user_id, "error_kind": "ai_limit"})
        await message.answer(texts.AI_LIMIT)
        return
    try:
        analysis = await _vision_call(deps.vision.correct(meal.analysis, text))
    except VisionError as e:
        no_credits = isinstance(e, VisionUnavailable) and e.no_credits
        log.warning(
            "vision unavailable",
            extra={"user_id": user_id, "error_kind": "no_credits" if no_credits else "vision_unavailable"},
        )
        await message.answer(texts.OVERLOADED)
        if no_credits:
            await _alert_no_credits(message.bot, deps)
        return
    if not _is_food(analysis):
        await message.answer(texts.CORRECTION_NOT_FOOD)
        return
    meal.analysis = analysis
    if current == MealEdit.sauce.state:
        meal.fat_answer = "sauce"
    await deps.storage.update_meal(meal)
    out, markup = _render(meal, await _remaining(deps, user, meal.local_date), _note(meal))
    await message.answer(out, reply_markup=markup)


_MENU_TEXTS = (texts.BTN_TODAY, texts.BTN_WEIGH, texts.BTN_PROFILE, texts.BTN_HELP)


async def leave_meal_edit(message: Message, state: FSMContext) -> None:
    """Кнопка меню или команда вместо поправки: выходим из правки и обрабатываем как обычно."""
    await state.clear()
    raise SkipHandler


def build_escape_router() -> Router:
    """Ставится до команд (/stop_tips и т. п.), чтобы команда тоже закрывала правку записи."""
    r = Router(name="meal_edit_escape")
    r.message.register(
        leave_meal_edit,
        StateFilter(MealEdit.correction, MealEdit.sauce),
        F.text.startswith("/") | F.text.in_(_MENU_TEXTS),
    )
    return r


def build_router() -> Router:
    r = Router(name="meals")
    r.message.register(
        on_correction_text,
        MealEdit.correction,
        F.text & ~F.text.startswith("/") & ~F.text.in_(_MENU_TEXTS),
    )
    r.message.register(
        on_correction_text,
        MealEdit.sauce,
        F.text & ~F.text.startswith("/") & ~F.text.in_(_MENU_TEXTS),
    )
    r.message.register(on_photo, F.photo | F.document.mime_type.startswith("image/"))
    r.callback_query.register(on_fat, F.data.startswith(keyboards.CB_FAT))
    r.callback_query.register(on_edit, F.data.startswith(keyboards.CB_MEAL_EDIT))
    r.callback_query.register(on_delete, F.data.startswith(keyboards.CB_MEAL_DEL))
    r.callback_query.register(on_type_menu, F.data.startswith(keyboards.CB_MEAL_TYPE))
    r.callback_query.register(on_type_chosen, F.data.startswith(keyboards.CB_MT))
    return r
