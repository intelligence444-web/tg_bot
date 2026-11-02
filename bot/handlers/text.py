"""Маршрутизатор текста (v1.1 6, 7, 13): шаблоны кодом, затем один запрос к модели.

Текст вне состояний FSM, не команда и не кнопка меню: строка профиля → вес («85», «вес 85,5»)
→ шаги → единица лимита ИИ → запрос «маршрутизатор-собеседник» (справка + история
переписки) → по ``kind``: еда (общий путь ``meals.record_meal``), рацион, ответ собеседника,
уточняющий вопрос. В лог не пишутся ни текст пользователя, ни справка, ни ответ модели.

Кнопка темы ``topic:{n}`` (v1.1 8) — тот же путь с вопросом «Расскажи коротко: <тема>».
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import assistant, keyboards, texts
from bot.assistant import DayContext, Plan, Routed
from bot.calc import effective_norm
from bot.diary import to_local
from bot.handlers.activity import day_norm_kcal, record_steps, record_training
from bot.handlers.meals import VISION_BUDGET_S, _alert_no_credits, _is_food, record_meal
from bot.handlers.onboarding import handle_profile_text
from bot.handlers.weight import ask_profile, load_profile_user, today_for, weighing_reply
from bot.limits import take_ai
from bot.models import ChatMessage, User
from bot.profile_line import parse_steps, parse_weight_text
from bot.progress import trend_weight
from bot.vision import BadResponse, VisionClient, VisionError, VisionUnavailable

if TYPE_CHECKING:
    from bot.app import Deps

log = logging.getLogger(__name__)

TG_LIMIT = 4096  # знаков в сообщении Telegram
PART_LIMIT = assistant.TG_PART  # части длинного ответа — с запасом
WEEK_DAYS = 7
IN_NORM = 0.10  # день «в норме» — ±10% от нормы
MENU_TEXTS = (texts.BTN_TODAY, texts.BTN_WEIGH, texts.BTN_PROFILE, texts.BTN_HELP)
_TAG = re.compile(r"<[^>]+>")
# Строка перед текстом кнопки темы: рассказ, а не еда, активность или рацион.
TOPIC_INSTRUCTION = "Это тема для короткого рассказа, ответь видом chat."


# --- справка ---


async def build_day_context(deps: Deps, user: User, now: datetime) -> DayContext:
    """Справка для модели: все числа считает код (v1.1 7.1).

    Норма сегодня — ``day_norm_kcal`` (норма плюс кредит активности). Итоги недели — 7 полных
    дней до сегодняшнего, по дням с записями еды; кредит за неделю читается одним запросом.
    """
    local = to_local(now)
    today = local.date()
    uid = user.user_id
    has_norm = user.profile is not None and user.norm is not None

    def norm_on(day: date, credit: float) -> float | None:
        return effective_norm(user, day).kcal + credit if has_norm else None

    meals = await deps.storage.meals_on(uid, today)
    meals.sort(key=lambda m: m.eaten_at)
    eaten = (
        sum(m.analysis.kcal for m in meals),
        sum(m.analysis.protein for m in meals),
        sum(m.analysis.fat for m in meals),
        sum(m.analysis.carbs for m in meals),
    )
    norm_kcal = await day_norm_kcal(deps, user, today)
    norm_protein = effective_norm(user, today).protein_g if has_norm else None

    week_start, week_end = today - timedelta(days=WEEK_DAYS), today - timedelta(days=1)
    by_day: dict[date, list[float]] = {}
    for m in await deps.storage.meals_between(uid, week_start, week_end):
        kp = by_day.setdefault(m.local_date, [0.0, 0.0])
        kp[0] += m.analysis.kcal
        kp[1] += m.analysis.protein
    credits: dict[date, float] = {}
    for a in await deps.storage.activities_between(uid, week_start, week_end):
        credits[a.local_date] = credits.get(a.local_date, 0.0) + a.kcal_credit
    week_avg_kcal = week_avg_protein = None
    in_norm = 0
    if by_day:
        week_avg_kcal = sum(k for k, _ in by_day.values()) / len(by_day)
        week_avg_protein = sum(p for _, p in by_day.values()) / len(by_day)
        for day, (kcal, _) in by_day.items():
            norm = norm_on(day, credits.get(day, 0.0))
            if norm and abs(kcal - norm) <= IN_NORM * norm:
                in_norm += 1

    trend = week_change = None
    if user.profile is not None:
        weighings = await deps.storage.weights(uid)
        trend = trend_weight(weighings, today)
        before = trend_weight(weighings, today - timedelta(days=7))
        if trend is not None and before is not None:
            week_change = trend - before
    target = user.profile.target_weight_kg if user.profile is not None else None

    return DayContext(
        profile=user.profile,
        trend_kg=trend,
        week_change_kg=week_change,
        target_kg=target,
        norm_kcal=norm_kcal,
        norm_protein=norm_protein,
        eaten=eaten,
        meals=tuple((m.meal_type, m.analysis.kcal) for m in meals),
        week_avg_kcal=week_avg_kcal,
        week_avg_protein=week_avg_protein,
        week_days_in_norm=in_norm,
        now_local=local,
    )


# --- отправка ---


def split_html(text: str, limit: int = PART_LIMIT) -> list[str]:
    """Готовый HTML (рацион) → части ≤ ``limit`` по границам строк.

    Строки рациона не содержат незакрытых тегов, так что любая граница строки безопасна.
    """
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(line) > limit:  # одна строка длиннее лимита — режем жёстко (на практике нет)
            parts.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        parts.append(current)
    return parts


async def _send_html(message: Message, parts: list[str], prefix: str) -> None:
    """Части HTML; ``prefix`` (простой текст) экранируется и ставится первой строкой."""
    if prefix:
        head = html.escape(prefix, quote=False)
        if parts and len(head) + 2 + len(parts[0]) <= TG_LIMIT:
            parts = [f"{head}\n\n{parts[0]}", *parts[1:]]
        else:
            parts = [head, *parts]
    for part in parts:
        await message.answer(part, parse_mode="HTML")


async def _send_plain(message: Message, text: str, prefix: str) -> None:
    await message.answer(f"{prefix}\n\n{text}" if prefix else text)


def _plain(html_text: str) -> str:
    return html.unescape(_TAG.sub("", html_text))


async def _typing(message: Message) -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except TelegramAPIError:
        pass  # «печатает…» — только подсказка


# --- запрос к модели ---


@dataclass
class _Outcome:
    routed: Routed | None  # None — модель дважды ответила не по формату
    plan_deviation: float | None = None  # рацион не сошёлся и после повтора


async def _route(
    client: VisionClient, messages: list[dict[str, str]], norm: tuple[float, float] | None
) -> _Outcome:
    """Запрос маршрутизатора: неверный формат → один повтор; рацион не сошёлся → один повтор."""
    routed: Routed | None = None
    raw = ""
    for _ in range(2):
        raw = await client.complete(assistant.SYSTEM_PROMPT, messages)
        try:
            routed = assistant.parse_routed(raw)
            break
        except BadResponse:
            log.warning("router bad response", extra={"error_kind": "bad_response"})
    if routed is None:
        return _Outcome(None)
    if routed.kind != "plan" or norm is None:
        return _Outcome(routed)

    ok, problem, deviation = assistant.check_plan(routed.plan, *norm)
    if ok:
        return _Outcome(routed)
    retry = [*messages, {"role": "assistant", "content": raw}, {"role": "user", "content": problem}]
    try:
        second = assistant.parse_routed(await client.complete(assistant.SYSTEM_PROMPT, retry))
    except BadResponse:
        log.warning("plan retry bad response", extra={"error_kind": "bad_response"})
        second = None
    if second is None or second.kind != "plan":
        return _Outcome(routed, deviation)
    ok, _, deviation = assistant.check_plan(second.plan, *norm)
    return _Outcome(second, None if ok else deviation)


async def _save_pair(deps: Deps, user_id: int, question: str, answer: str) -> None:
    now = deps.clock()
    await deps.storage.add_chat_message(user_id, ChatMessage("user", question, now))
    await deps.storage.add_chat_message(user_id, ChatMessage("assistant", answer, now))


async def handle_text(
    message: Message,
    state: FSMContext,
    deps: Deps,
    text: str,
    *,
    prefix: str = "",
    user_id: int | None = None,
    topic: str | None = None,
    ai_taken: bool = False,
) -> None:
    """Текст пользователя (набранный или распознанный голос) по порядку v1.1 6.

    ``prefix`` — простой текст (не HTML) первой строкой ответа, например «🎙 Понял так: …»;
    для ответов в HTML он экранируется здесь. ``user_id`` — когда ``message`` не от пользователя
    (сообщение бота под нажатой кнопкой). ``topic`` — нажата кнопка темы: без шаблонов кодом,
    модель просят ответить видом chat; любой другой ответ ничего не записывает, а пользователь
    получает ``texts.TOPIC_FALLBACK``. ``ai_taken`` — единица лимита уже списана вызывающим.
    """
    if user_id is None:
        user_id = message.from_user.id
    text = text.strip()

    # 1. Шаблоны кодом — без ИИ и без лимита.
    if topic is None:
        if await handle_profile_text(message, state, deps, text):
            return
        kg = parse_weight_text(text)
        if kg is not None:
            await _weigh(message, state, deps, kg)
            return
        steps = parse_steps(text)
        if steps is not None:
            await record_steps(message, deps, steps, prefix=prefix)
            return

    # 2. Лимит ИИ: единица тратится до вызова модели.
    now = deps.clock()
    today = to_local(now).date()
    if not ai_taken and not await take_ai(deps.storage, user_id, today, deps.ai_limit):
        log.info("ai limit reached", extra={"user_id": user_id, "error_kind": "ai_limit"})
        await _send_plain(message, texts.AI_LIMIT, prefix)
        return

    user = await deps.storage.get_user(user_id) or User(user_id=user_id)
    ctx = await build_day_context(deps, user, now)
    history = await deps.storage.recent_chat(user_id, now)
    messages = [{"role": m.role, "content": m.text} for m in history]
    current = f"{assistant.build_context_text(ctx)}\n\n"
    if topic is not None:
        current += f"{TOPIC_INSTRUCTION}\n"
    current += f"Сообщение пользователя: {text}"
    messages.append({"role": "user", "content": current})
    norm = None
    if ctx.norm_kcal is not None and ctx.norm_protein is not None:
        norm = (ctx.norm_kcal, ctx.norm_protein)
    # Тема: рацион не нужен — без проверки и повтора рациона (один запрос к модели).
    check_norm = None if topic is not None else norm

    await _typing(message)
    client = deps.chat or deps.vision
    try:
        try:
            async with asyncio.timeout(VISION_BUDGET_S):
                outcome = await _route(client, messages, check_norm)
        except TimeoutError as e:
            raise VisionUnavailable("router time budget exceeded", no_credits=False) from e
    except VisionError as e:
        no_credits = isinstance(e, VisionUnavailable) and e.no_credits
        log.warning(
            "router unavailable",
            extra={"user_id": user_id, "error_kind": "no_credits" if no_credits else "vision_unavailable"},
        )
        await _send_plain(message, texts.OVERLOADED, prefix)
        if no_credits:
            await _alert_no_credits(message.bot, deps)
        return

    routed = outcome.routed
    if topic is not None:
        await _reply_topic(message, deps, user_id, routed, text, topic, prefix)
        return
    if routed is None:
        await _send_plain(message, texts.NOT_UNDERSTOOD, prefix)
        return
    log.info("text routed: %s", routed.kind, extra={"user_id": user_id})

    if routed.kind == "food":
        if not _is_food(routed.analysis):
            await _send_plain(message, texts.NOT_FOOD_TEXT, prefix)
            return
        # Запрос шёл до полутора минут — пишем поверх свежей записи пользователя.
        fresh = await deps.storage.get_user(user_id) or User(user_id=user_id)
        await record_meal(message, deps, fresh, routed.analysis, status_msg=None, prefix=prefix)
        return
    if routed.kind == "activity":
        await record_training(message, deps, routed.activity_name, routed.minutes, routed.met, prefix=prefix)
        return
    if routed.kind == "plan":
        await _reply_plan(
            message, deps, user, routed.plan, outcome.plan_deviation, text, prefix, has_norm=norm is not None
        )
        return
    # chat / unclear
    parts = assistant.to_telegram_html(routed.reply)
    if not parts:
        await _send_plain(message, texts.NOT_UNDERSTOOD, prefix)
        return
    await _send_html(message, parts, prefix)
    await _save_pair(deps, user_id, text, routed.reply)


async def _reply_topic(
    message: Message, deps: Deps, user_id: int, routed: Routed | None, question: str, topic: str, prefix: str
) -> None:
    """Ответ на тему — только рассказ (chat или unclear с текстом); иначе ничего не пишем
    и предлагаем спросить своими словами."""
    log.info("topic routed: %s", routed.kind if routed else "bad_response", extra={"user_id": user_id})
    parts: list[str] = []
    if routed is not None and routed.kind in ("chat", "unclear") and routed.reply.strip():
        parts = assistant.to_telegram_html(routed.reply)
    if not parts:
        await _send_plain(message, texts.TOPIC_FALLBACK.format(topic=topic), prefix)
        return
    await _send_html(message, parts, prefix)
    await _save_pair(deps, user_id, question, routed.reply)


async def _reply_plan(
    message: Message,
    deps: Deps,
    user: User,
    plan: Plan,
    deviation: float | None,
    question: str,
    prefix: str,
    *,
    has_norm: bool,
) -> None:
    if user.profile is None or not has_norm:
        # Инструкция модели и так просит в этом случае ответ «chat»; здесь — страховка.
        await _send_plain(message, texts.PROFILE_HINT, prefix)
        return
    formatted = assistant.format_plan(plan, deviation)
    await _send_html(message, split_html(formatted), prefix)
    await _save_pair(deps, user.user_id, question, _plain(formatted))


async def _weigh(message: Message, state: FSMContext, deps: Deps, kg: float) -> None:
    """«85» вне состояний — как кнопка «⚖️» и ввод числа."""
    user = await load_profile_user(deps, message.from_user.id)
    if user is None:
        await ask_profile(message, state)
        return
    reply, markup = await weighing_reply(user, today_for(deps), kg, deps)
    await message.answer(reply, reply_markup=markup)


# --- роутер ---


async def _no_state(message: Message, state: FSMContext) -> bool:
    """Нет состояния FSM — по текущему значению, а не по ``raw_state`` апдейта: обработчики
    выше (например, выход из ввода веса) могли сбросить состояние и передать сообщение дальше."""
    return await state.get_state() is None


def _is_plain_text(message: Message) -> bool:
    t = message.text
    return t is not None and not t.startswith("/") and t not in MENU_TEXTS


async def on_text(message: Message, state: FSMContext, deps: Deps) -> None:
    await handle_text(message, state, deps, message.text)


async def on_topic(callback: CallbackQuery, state: FSMContext, deps: Deps) -> None:
    """Кнопка «💡 <тема>»: вопрос собеседнику, ровно одна единица лимита.

    Лимит проверяется первым: исчерпан — кнопка и номер темы не меняются, ответ ``AI_LIMIT``.
    Единицу списывает этот обработчик, ``handle_text`` её не трогает (``ai_taken=True``).
    """
    n = keyboards.parse_topic(callback.data)
    await callback.answer()
    if n is None or not isinstance(callback.message, Message):
        return
    user_id = callback.from_user.id
    user = await deps.storage.get_user(user_id)
    if user is None:
        return
    if not await take_ai(deps.storage, user_id, to_local(deps.clock()).date(), deps.ai_limit):
        log.info("ai limit reached", extra={"user_id": user_id, "error_kind": "ai_limit"})
        await callback.message.answer(texts.AI_LIMIT)
        return
    if user.next_topic <= n:
        user.next_topic = n + 1
        await deps.storage.save_user(user)
    # Кнопку убираем, чтобы повторное нажатие не тратило ещё запрос.
    markup = callback.message.reply_markup
    if markup is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboards.without_topic(markup))
        except TelegramAPIError:
            pass  # не изменилось или сообщение старое — ответ всё равно придёт
    topic = texts.TOPICS[n % len(texts.TOPICS)]
    await handle_text(
        callback.message,
        state,
        deps,
        texts.TOPIC_QUESTION.format(topic=topic),
        user_id=user_id,
        topic=topic,
        ai_taken=True,
    )


def build_router() -> Router:
    r = Router(name="text")
    r.message.register(on_text, _is_plain_text, _no_state)
    r.callback_query.register(on_topic, F.data.startswith(keyboards.CB_TOPIC))
    return r
