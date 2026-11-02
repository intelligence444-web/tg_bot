"""Клавиатуры. Форматы callback_data здесь — контракт для обработчиков."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot import texts

CB_CONSENT = "consent"
CB_PROFILE_YES = "pf:yes"
CB_PROFILE_NO = "pf:no"
CB_ACT = "act:"  # act:{activity}
CB_GOAL = "goal:"  # goal:{lose|maintain|gain}
CB_BMI_RETRY = "bmi:retry"
CB_BMI_OK = "bmi:ok"


def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_TODAY), KeyboardButton(text=texts.BTN_WEIGH)],
            [KeyboardButton(text=texts.BTN_PROFILE), KeyboardButton(text=texts.BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def consent() -> InlineKeyboardMarkup:
    return _inline([[(texts.BTN_CONSENT, CB_CONSENT)]])


def profile_confirm() -> InlineKeyboardMarkup:
    return _inline([[(texts.BTN_PROFILE_YES, CB_PROFILE_YES), (texts.BTN_PROFILE_FIX, CB_PROFILE_NO)]])


def activity() -> InlineKeyboardMarkup:
    return _inline([[(label, f"{CB_ACT}{key}")] for key, label in texts.ACTIVITY_LABELS.items()])


def goal() -> InlineKeyboardMarkup:
    return _inline([[(label, f"{CB_GOAL}{key}")] for key, label in texts.GOAL_LABELS.items()])


def bmi_low() -> InlineKeyboardMarkup:
    return _inline([[(texts.BTN_BMI_RETRY, CB_BMI_RETRY), (texts.BTN_BMI_OK, CB_BMI_OK)]])


# --- Приём пищи (спец. 6.4) ---
CB_FAT = "fat:"  # fat:{meal_id}:{none|little|much|sauce}
CB_MEAL_EDIT = "meal:edit:"  # meal:edit:{meal_id}
CB_MEAL_DEL = "meal:del:"  # meal:del:{meal_id}
CB_MEAL_TYPE = "meal:type:"  # meal:type:{meal_id}
CB_MT = "mt:"  # mt:{meal_id}:{breakfast|lunch|snack|dinner}
# Активность (v1.1 9). Не «act:del:» — префикс «act:» занят кнопками уровня активности анкеты.
CB_ACT_DEL = "actdel:"  # actdel:{activity_id}
# Предложение темы (v1.1 8): n — номер темы у пользователя, название — ``texts.TOPICS[n % 30]``.
CB_TOPIC = "topic:"  # topic:{n}


def meal_result(meal_id: str, ask_fat: bool, topic: int | None = None) -> InlineKeyboardMarkup:
    """Кнопки под едой; ``topic`` — номер предложенной темы (кнопка «💡 <тема>» последней строкой)."""
    rows: list[list[tuple[str, str]]] = []
    if ask_fat:
        rows.append(
            [
                (texts.BTN_FAT_NONE, f"{CB_FAT}{meal_id}:none"),
                (texts.BTN_FAT_LITTLE, f"{CB_FAT}{meal_id}:little"),
            ]
        )
        rows.append(
            [
                (texts.BTN_FAT_MUCH, f"{CB_FAT}{meal_id}:much"),
                (texts.BTN_FAT_SAUCE, f"{CB_FAT}{meal_id}:sauce"),
            ]
        )
    rows.append(
        [
            (texts.BTN_MEAL_EDIT, f"{CB_MEAL_EDIT}{meal_id}"),
            (texts.BTN_MEAL_DELETE, f"{CB_MEAL_DEL}{meal_id}"),
        ]
    )
    rows.append([(texts.BTN_MEAL_TYPE, f"{CB_MEAL_TYPE}{meal_id}")])
    if topic is not None:
        rows.append(_topic_row(topic))
    return _inline(rows)


def parse_topic(data: str | None) -> int | None:
    """``topic:{n}`` → n (n ≥ 0); иначе None."""
    if data is None or not data.startswith(CB_TOPIC):
        return None
    raw = data.removeprefix(CB_TOPIC)
    return int(raw) if raw.isascii() and raw.isdigit() else None


def topic_of(markup: InlineKeyboardMarkup | None) -> int | None:
    """Номер темы с кнопки «💡» в клавиатуре сообщения; кнопки нет — None."""
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for b in row:
            n = parse_topic(b.callback_data)
            if n is not None:
                return n
    return None


def without_topic(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows = [
        [b for b in row if parse_topic(b.callback_data) is None] for row in markup.inline_keyboard
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def activity_result(activity_id: str) -> InlineKeyboardMarkup:
    return _inline([[(texts.BTN_ACTIVITY_DELETE, f"{CB_ACT_DEL}{activity_id}")]])


def _topic_row(topic: int) -> list[tuple[str, str]]:
    return [(texts.BTN_TOPIC.format(topic=texts.TOPICS[topic % len(texts.TOPICS)]), f"{CB_TOPIC}{topic}")]


def meal_types(meal_id: str, topic: int | None = None) -> InlineKeyboardMarkup:
    """Выбор приёма пищи; ``topic`` — кнопка темы остаётся под сообщением."""
    labels = texts.MEAL_LABELS
    rows = [
        [(labels["breakfast"], f"{CB_MT}{meal_id}:breakfast"), (labels["lunch"], f"{CB_MT}{meal_id}:lunch")],
        [(labels["snack"], f"{CB_MT}{meal_id}:snack"), (labels["dinner"], f"{CB_MT}{meal_id}:dinner")],
    ]
    if topic is not None:
        rows.append(_topic_row(topic))
    return _inline(rows)


# --- Вес, прогресс, профиль (спец. 6.6–6.9, 9) ---
CB_CHART = "chart"
CB_GOAL_DONE = "goal_done:"  # goal_done:{maintain|new}
CB_CORRIDOR = "corridor:"  # corridor:{back|keep}
CB_NORM_RECALC = "norm:recalc"
CB_PROFILE_GOAL = "profile:goal"
CB_PROFILE_RESTART = "profile:restart"


def weighing_result(
    *, goal_done: bool = False, corridor_target: str | None = None, recalc: bool = False
) -> InlineKeyboardMarkup:
    """Кнопки под ответом на взвешивание; ``corridor_target`` — желаемый вес текстом для кнопки."""
    rows: list[list[tuple[str, str]]] = []
    if goal_done:
        rows.append([(texts.BTN_GOAL_MAINTAIN, f"{CB_GOAL_DONE}maintain"), (texts.BTN_GOAL_NEW, f"{CB_GOAL_DONE}new")])
    if corridor_target is not None:
        rows.append(
            [
                (texts.BTN_CORRIDOR_BACK.format(target=corridor_target), f"{CB_CORRIDOR}back"),
                (texts.BTN_CORRIDOR_KEEP, f"{CB_CORRIDOR}keep"),
            ]
        )
    if recalc:
        rows.append([(texts.BTN_NORM_RECALC, CB_NORM_RECALC)])
    rows.append([(texts.BTN_CHART, CB_CHART)])
    return _inline(rows)


def profile(corridor_target: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if corridor_target is not None:
        rows.append(
            [
                (texts.BTN_CORRIDOR_BACK.format(target=corridor_target), f"{CB_CORRIDOR}back"),
                (texts.BTN_CORRIDOR_KEEP, f"{CB_CORRIDOR}keep"),
            ]
        )
    rows.append([(texts.BTN_CHART, CB_CHART), (texts.BTN_PROFILE_GOAL, CB_PROFILE_GOAL)])
    rows.append([(texts.BTN_PROFILE_RESTART, CB_PROFILE_RESTART)])
    return _inline(rows)
