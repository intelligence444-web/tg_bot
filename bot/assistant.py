"""ИИ-собеседник (v1.1 6, 7): справка, системная инструкция, разбор ответа, рацион, HTML."""
from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from bot import texts
from bot.calc import format_num
from bot.models import Analysis, MealType, Profile
from bot.vision import ANALYSIS_JSON, BadResponse, MAX_COMMENT, parse_analysis

RoutedKind = Literal["food", "activity", "plan", "chat", "unclear"]

MAX_REPLY = 1200  # знаков в ответе собеседника (v1.1 7.2)
TG_PART = 4000  # знаков в одном сообщении Telegram (с запасом до 4096)
MIN_MINUTES, MAX_MINUTES = 1, 600
MIN_MET, MAX_MET = 1, 20
PLAN_KCAL_TOLERANCE = 10.0  # % от нормы по сумме первых вариантов
PLAN_PROTEIN_MIN = 0.9  # доля нормы белка
OPTION_MACRO_TOLERANCE = 0.15  # |ккал − (4Б + 4У + 9Ж)| ≤ 15% от ккал

_MEAL_TYPES: tuple[MealType, ...] = ("breakfast", "lunch", "snack", "dinner")


# ---------- справка ----------

@dataclass(frozen=True)
class DayContext:
    profile: Profile | None
    trend_kg: float | None
    week_change_kg: float | None
    target_kg: float | None
    norm_kcal: float | None
    norm_protein: float | None
    eaten: tuple[float, float, float, float]  # ккал, Б, Ж, У
    meals: tuple[tuple[str, float], ...]  # (тип, ккал)
    week_avg_kcal: float | None
    week_avg_protein: float | None
    week_days_in_norm: int
    now_local: datetime


_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def _signed(x: float, ndigits: int = 1) -> str:
    s = format_num(abs(x), ndigits)
    if s == "0":
        return s
    return ("+" if x > 0 else "−") + s


def build_context_text(ctx: DayContext) -> str:
    """Справка для модели (v1.1 7.1). Все числа считает код."""
    now = ctx.now_local
    lines = [
        "Справка из бота (числа посчитал бот, бери их отсюда):",
        f"Сейчас: {now:%d.%m.%Y}, {_WEEKDAYS[now.weekday()]}, {now:%H:%M} по Москве",
    ]
    p = ctx.profile
    if p is None:
        lines.append("Профиля нет — норма не рассчитана")
    else:
        weight = ctx.trend_kg if ctx.trend_kg is not None else p.weight_kg
        age_word = texts.plural(p.age, *texts.YEAR_WORDS)
        goal = texts.GOAL_LABELS[p.goal].lower()
        if p.target_weight_kg is not None:
            goal += f", желаемый вес {format_num(p.target_weight_kg, 1)} кг"
        lines.append(
            f"Профиль: {texts.SEX_LABELS[p.sex]} пол, {p.age} {age_word}, "
            f"рост {format_num(p.height_cm)} см, вес по тренду {format_num(weight, 1)} кг, "
            f"активность: {texts.ACTIVITY_LABELS[p.activity].lower()}, цель: {goal}"
        )

    kcal, protein, fat, carbs = ctx.eaten
    if ctx.norm_kcal is not None:
        norm = f"Норма на сегодня (с учётом активности): {format_num(ctx.norm_kcal)} ккал"
        if ctx.norm_protein is not None:
            norm += f", белок {format_num(ctx.norm_protein)} г"
        lines.append(norm)
    lines.append(
        f"Съедено: {format_num(kcal)} ккал, Б {format_num(protein)} г, "
        f"Ж {format_num(fat)} г, У {format_num(carbs)} г"
    )
    if ctx.norm_kcal is not None:
        left = ctx.norm_kcal - kcal
        if round(left) >= 0:
            rest = f"Осталось: {format_num(left)} ккал"
        else:
            rest = f"Осталось: 0 ккал (сверх нормы на {format_num(-left)} ккал)"
        if ctx.norm_protein is not None:
            rest += f", белок {format_num(max(0.0, ctx.norm_protein - protein))} г"
        lines.append(rest)

    if ctx.meals:
        meals = "; ".join(
            f"{texts.MEAL_TYPE_NAMES.get(t, t)} — {format_num(k)} ккал" for t, k in ctx.meals
        )
        lines.append(f"Приёмы пищи сегодня: {meals}")
    else:
        lines.append("Приёмы пищи сегодня: пока нет записей")

    if ctx.week_avg_kcal is None:
        lines.append("Последние 7 дней: записей еды нет")
    else:
        week = f"Последние 7 дней: в среднем {format_num(ctx.week_avg_kcal)} ккал"
        if ctx.week_avg_protein is not None:
            week += f" и {format_num(ctx.week_avg_protein)} г белка"
        week += f" в день, дней в норме ±10%: {ctx.week_days_in_norm}"
        lines.append(week)

    if ctx.trend_kg is not None:
        trend = f"Вес: по тренду {format_num(ctx.trend_kg, 1)} кг"
        if ctx.week_change_kg is not None:
            trend += f", за неделю {_signed(ctx.week_change_kg)} кг"
        if ctx.target_kg is not None:
            trend += f", до цели {format_num(abs(ctx.trend_kg - ctx.target_kg), 1)} кг"
        lines.append(trend)
    elif p is not None:
        lines.append("Вес: взвешиваний пока нет")
    return "\n".join(lines)


# ---------- системная инструкция ----------

SYSTEM_PROMPT = f"""Ты — помощник по питанию в Telegram-боте, который считает калории и ведёт дневник еды. Обращайся к пользователю на «ты», тепло и по-дружески. Посыл бота — питание под цель без запретов и без голода: не запрещай, не упрекай и не стыди, предлагай мягкие шаги. Не используй слова «нельзя», «запрещено», «сорвался», «плохо». В ответе не больше двух эмодзи.

Каждое сообщение пользователя начинается со справки из бота: профиль, норма дня с учётом активности, съедено и осталось, приёмы пищи сегодня, итоги последних 7 дней, тренд веса, дата и время по Москве. После справки идёт сам текст пользователя. Отвечай на текст пользователя, справку используй как данные.

КАК ВЫБРАТЬ ВИД ОТВЕТА ("kind") — ровно один:
- "food" — пользователь сообщает, что съел или выпил (или просит записать еду): «съел 2 яйца и тост», «на обед борщ и хлеб», «выпил латте 300 мл». С граммами или без: если веса нет, оцени обычную порцию.
- "activity" — пользователь сообщает о физической активности или тренировке, которую уже сделал, и понятна длительность: «пробежал 30 минут», «час плавал», «45 минут силовой в зале».
- "plan" — пользователь просит составить меню или рацион на день: «составь меню на завтра», «распиши, что мне съесть сегодня», «рацион без молочки».
- "chat" — вопрос, просьба о совете или разговор о питании, весе, активности, сне и самочувствии, связанном с едой: «сколько калорий в банане?», «почему стоит вес?», «что съесть на ужин?». Вопрос о еде — это chat, а не food: food только когда пользователь рассказывает о съеденном. Сообщения на другие темы — тоже chat (см. правило 5).
- "unclear" — непонятно, что имеет в виду пользователь, или для записи не хватает деталей (например, «сходил в зал» без длительности). Тогда задай один короткий уточняющий вопрос.

ПРАВИЛА ДЛЯ ТЕКСТА ОТВЕТА ("reply" в chat и unclear):
1. Числа нормы, остатка и съеденного бери только из справки. Норму сам не пересчитывай и не предлагай другую; если в справке профиля нет — предложи заполнить профиль в боте.
2. Не ставь диагнозов, не советуй лекарства, БАДы и спортивное питание с дозировками.
3. Если речь о симптомах, беременности, кормлении грудью, хронических болезнях или пользователю меньше 18 лет — мягко посоветуй обсудить питание с врачом.
4. Если видишь признаки расстройства пищевого поведения (голодовки, вызывание рвоты, наказание себя едой или нагрузкой, страх еды) — не давай никаких советов по дефициту и снижению калорий; бережно, без оценок, поддержи и предложи обратиться к специалисту по пищевому поведению (психологу или врачу). В таком случае выбирай "chat", даже если в сообщении есть еда.
5. Если тема не про питание, вес, активность, сон или самочувствие, связанное с едой, — вежливо верни разговор к питанию одной фразой.
6. "reply" — до {MAX_REPLY} знаков, простой текст без Markdown (без **, #, таблиц); списки — строками, которые начинаются с «- ».
7. Когда уместно, в конце предложи следующий шаг в боте: сфотографировать или описать еду, взвеситься, попросить рацион на день.

ФОРМАТ ОТВЕТА. Отвечай строго одним JSON-объектом, без пояснений, без markdown и без текста до или после. Для каждого вида — своя схема:

1) Еда:
{{"kind": "food", "analysis": {{…}}}}
Объект "analysis" — оценка всего съеденного из сообщения в таком формате:
{ANALYSIS_JSON}
Для каждой позиции оцени вес в граммах и КБЖУ на этот вес. Масло, майонез и заправки, которые почти наверняка есть в блюде (жарка, паста, каши, гарниры, заправленные салаты), выдели отдельной позицией с "hidden": true и "category": "fat"; "ask_about_fat": true, если такой жир заметно меняет калорийность. "is_food": true. Граммы не больше 3000, ккал на позицию не больше 5000, все числа неотрицательные, названия по-русски. "comment" — одна добрая фраза до {MAX_COMMENT} знаков о белке, овощах, сладком или напитках, не больше одного эмодзи; если сказать нечего — пропусти поле.

2) Активность:
{{"kind": "activity", "name": "бег", "minutes": 30, "met": 8.0}}
"name" — короткое название по-русски, "minutes" — длительность в минутах (1–600), "met" — MET этого вида активности по справочнику физических нагрузок (1–20). Калории не считай — их считает бот.

3) Рацион на день:
{{"kind": "plan", "meals": [{{"type": "breakfast", "options": [{{"text": "Омлет из 3 яиц с помидорами, 2 цельнозерновых тоста", "kcal": 450, "protein": 30, "fat": 28, "carbs": 25}}]}}], "notes": "Белок равномерно по приёмам пищи"}}
Правила рациона:
- 3–4 приёма пищи, "type" — один из breakfast, lunch, dinner, snack; у каждого приёма 1–3 варианта на выбор.
- Цель — норма ккал и белка из справки: сумма первых вариантов всех приёмов в пределах ±10% от нормы ккал, белок не меньше 90% нормы белка.
- Учитывай исключения и пожелания из сообщения пользователя: продукты, которые он не ест, бюджет, время на готовку.
- "kcal", "protein", "fat", "carbs" — на весь вариант целиком, в ккал и граммах; у каждого варианта kcal ≈ 4·protein + 4·carbs + 9·fat (расхождение не больше 15%).
- "text" — блюдо с примерным весом или количеством, простым текстом; "notes" — одна-две короткие подсказки.

4) Ответ собеседника:
{{"kind": "chat", "reply": "текст ответа"}}

5) Уточняющий вопрос:
{{"kind": "unclear", "reply": "один короткий вопрос"}}"""


# ---------- разбор ответа модели ----------

@dataclass(frozen=True)
class PlanOption:
    text: str
    kcal: float
    protein: float
    fat: float
    carbs: float


@dataclass(frozen=True)
class PlanMeal:
    type: MealType
    options: tuple[PlanOption, ...]


@dataclass(frozen=True)
class Plan:
    meals: tuple[PlanMeal, ...]
    notes: str


@dataclass(frozen=True)
class Routed:
    kind: RoutedKind
    analysis: Analysis | None = None
    activity_name: str = ""
    minutes: float = 0
    met: float = 0
    plan: Plan | None = None
    reply: str = ""


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _num(value: Any, lo: float = 0.0, hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadResponse("not a number")
    if not math.isfinite(value) or value < lo or (hi is not None and value > hi):
        raise BadResponse("number out of range")
    return float(value)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadResponse("empty text")
    return value.strip()


def _parse_plan(data: dict) -> Plan:
    raw_meals = data.get("meals")
    if not isinstance(raw_meals, list) or not 3 <= len(raw_meals) <= 4:
        raise BadResponse("plan needs 3-4 meals")
    meals = []
    for m in raw_meals:
        if not isinstance(m, dict) or m.get("type") not in _MEAL_TYPES:
            raise BadResponse("bad meal")
        raw_opts = m.get("options")
        if not isinstance(raw_opts, list) or not 1 <= len(raw_opts) <= 3:
            raise BadResponse("meal needs 1-3 options")
        opts = []
        for o in raw_opts:
            if not isinstance(o, dict):
                raise BadResponse("bad option")
            try:
                opts.append(PlanOption(
                    _text(o["text"]), _num(o["kcal"]), _num(o["protein"]),
                    _num(o["fat"]), _num(o["carbs"]),
                ))
            except KeyError as e:
                raise BadResponse(f"missing field {e}") from e
        meals.append(PlanMeal(m["type"], tuple(opts)))
    notes = data.get("notes")
    return Plan(tuple(meals), notes.strip() if isinstance(notes, str) else "")


def parse_routed(raw: str) -> Routed:
    """Ответ маршрутизатора (v1.1 6) → ``Routed``; что угодно иное → ``BadResponse``."""
    text = raw.strip() if isinstance(raw, str) else ""
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise BadResponse("invalid json") from e
    if not isinstance(data, dict):
        raise BadResponse("not an object")
    kind = data.get("kind")
    if kind == "food":
        analysis = data.get("analysis")
        if not isinstance(analysis, dict):
            raise BadResponse("no analysis")
        return Routed("food", analysis=parse_analysis(json.dumps(analysis)))
    if kind == "activity":
        return Routed(
            "activity",
            activity_name=_text(data.get("name")),
            minutes=_num(data.get("minutes"), MIN_MINUTES, MAX_MINUTES),
            met=_num(data.get("met"), MIN_MET, MAX_MET),
        )
    if kind == "plan":
        return Routed("plan", plan=_parse_plan(data))
    if kind in ("chat", "unclear"):
        return Routed(kind, reply=_text(data.get("reply")))
    raise BadResponse("unknown kind")


# ---------- рацион ----------

def _macro_kcal(o: PlanOption) -> float:
    return 4 * o.protein + 4 * o.carbs + 9 * o.fat


def check_plan(plan: Plan, norm_kcal: float, norm_protein: float) -> tuple[bool, str, float]:
    """Проверка кодом (v1.1 7.3): (ок, что не сошлось — для повтора модели, отклонение ккал в %)."""
    firsts = [m.options[0] for m in plan.meals]
    total_kcal = sum(o.kcal for o in firsts)
    total_protein = sum(o.protein for o in firsts)
    deviation = (total_kcal - norm_kcal) / norm_kcal * 100 if norm_kcal > 0 else 0.0
    problems = []
    if abs(deviation) > PLAN_KCAL_TOLERANCE:
        problems.append(
            f"Сумма первых вариантов — {format_num(total_kcal)} ккал, а норма — "
            f"{format_num(norm_kcal)} ккал ({_signed(deviation, 0)}%); "
            f"нужно в пределах ±{format_num(PLAN_KCAL_TOLERANCE)}%."
        )
    if total_protein < PLAN_PROTEIN_MIN * norm_protein:
        problems.append(
            f"Белок в первых вариантах — {format_num(total_protein)} г, а нужно не меньше "
            f"{format_num(PLAN_PROTEIN_MIN * norm_protein)} г "
            f"({format_num(PLAN_PROTEIN_MIN * 100)}% от нормы {format_num(norm_protein)} г)."
        )
    for m in plan.meals:
        for o in m.options:
            if abs(o.kcal - _macro_kcal(o)) > OPTION_MACRO_TOLERANCE * o.kcal:
                problems.append(
                    f"«{o.text}»: указано {format_num(o.kcal)} ккал, а по БЖУ "
                    f"(4·protein + 4·carbs + 9·fat) выходит {format_num(_macro_kcal(o))} ккал."
                )
    if not problems:
        return True, "", deviation
    text = (
        "Рацион не сошёлся с проверкой:\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\nИсправь и верни полный JSON того же вида (kind \"plan\")."
    )
    return False, text, deviation


def format_plan(plan: Plan, deviation_pct: float | None) -> str:
    """Рацион для Telegram (HTML). ``deviation_pct`` не None — пометка «Суммы примерные»."""
    e = html.escape
    lines = [f"<b>{e(texts.PLAN_TITLE)}</b>"]
    for meal in plan.meals:
        first = meal.options[0]
        lines.append("")
        lines.append("<b>" + e(texts.PLAN_MEAL_HEAD.format(
            name=texts.PLAN_MEAL_NAMES[meal.type],
            kcal=format_num(first.kcal), protein=format_num(first.protein),
        )) + "</b>")
        for n, o in enumerate(meal.options, 1):
            lines.append(e(texts.PLAN_OPTION.format(
                n=n, text=o.text, kcal=format_num(o.kcal), protein=format_num(o.protein),
                fat=format_num(o.fat), carbs=format_num(o.carbs),
            )))
    firsts = [m.options[0] for m in plan.meals]
    lines.append("")
    lines.append(e(texts.PLAN_TOTAL.format(
        kcal=format_num(sum(o.kcal for o in firsts)),
        protein=format_num(sum(o.protein for o in firsts)),
        fat=format_num(sum(o.fat for o in firsts)),
        carbs=format_num(sum(o.carbs for o in firsts)),
    )))
    if plan.notes:
        lines.append("")
        lines.append(e(plan.notes))
    if deviation_pct is not None:
        lines.append("")
        lines.append(e(texts.PLAN_APPROX.format(pct=format_num(abs(deviation_pct)))))
    return "\n".join(lines)


# ---------- HTML для Telegram ----------

_HEADING = re.compile(r"(?m)^[ \t]*#+[ \t]*")
_STAR_BULLET = re.compile(r"(?m)^([ \t]*)\*[ \t]+")
_BOLD = re.compile(r"\*\*(?=\S)([^\n]*?\S)\*\*")
_PARAGRAPHS = re.compile(r"\n[ \t]*\n\s*")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _convert(raw: str) -> str:
    """Markdown → безопасный HTML Telegram: только ``<b>``, остальное экранировано или убрано."""
    s = _HEADING.sub("", raw)
    s = s.replace("#", "").replace("__", "").replace("`", "")
    s = _STAR_BULLET.sub(r"\1- ", s)
    s = html.escape(s, quote=False)
    s = _BOLD.sub(r"<b>\1</b>", s)
    return s.replace("**", "")


def _hard_pieces(raw: str) -> list[tuple[str, str]]:
    """Режет кусок без подходящих границ так, чтобы каждая часть после ``_convert`` ≤ TG_PART."""
    pieces: list[tuple[str, str]] = []
    sep = ""
    rest = raw
    while rest:
        n = min(len(rest), TG_PART)
        while (size := len(_convert(rest[:n]))) > TG_PART:
            n = max(1, min(n - 1, n * TG_PART // size))
        if n < len(rest):
            cut = rest.rfind(" ", n // 2, n)
            if cut > 0:
                pieces.append((sep, rest[:cut]))
                sep, rest = " ", rest[cut + 1:]
                continue
        pieces.append((sep, rest[:n]))
        sep, rest = "", rest[n:]
    return pieces


def to_telegram_html(text: str) -> list[str]:
    """Ответ модели → части HTML ≤ 4000 знаков, по абзацам, затем по предложениям, затем жёстко."""
    pieces: list[tuple[str, str]] = []  # (разделитель перед куском, исходный текст куска)
    paragraphs = [p.strip() for p in _PARAGRAPHS.split(text.strip()) if p.strip()]
    for i, para in enumerate(paragraphs):
        para_sep = "\n\n" if i else ""
        if len(_convert(para)) <= TG_PART:
            pieces.append((para_sep, para))
            continue
        for j, sentence in enumerate(_SENTENCE_END.split(para)):
            sep = para_sep if j == 0 else " "
            if len(_convert(sentence)) <= TG_PART:
                pieces.append((sep, sentence))
                continue
            hard = _hard_pieces(sentence)
            pieces.append((sep, hard[0][1]))
            pieces.extend(hard[1:])

    parts: list[str] = []
    current = ""
    for sep, raw in pieces:
        converted = _convert(raw)
        if not converted.strip():
            continue
        if current and len(current) + len(sep) + len(converted) <= TG_PART:
            current += sep + converted
        else:
            if current:
                parts.append(current)
            current = converted
    if current:
        parts.append(current)
    return parts
