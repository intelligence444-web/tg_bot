"""Клавиатуры. Форматы callback_data здесь — контракт для обработчиков."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot import texts

CB_CONSENT = "consent"
CB_SEX = "sex:"  # sex:{male|female}
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


def sex() -> InlineKeyboardMarkup:
    return _inline([[(texts.BTN_MALE, f"{CB_SEX}male"), (texts.BTN_FEMALE, f"{CB_SEX}female")]])


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


def meal_result(meal_id: str, ask_fat: bool) -> InlineKeyboardMarkup:
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
    return _inline(rows)


def meal_types(meal_id: str) -> InlineKeyboardMarkup:
    labels = texts.MEAL_LABELS
    return _inline(
        [
            [(labels["breakfast"], f"{CB_MT}{meal_id}:breakfast"), (labels["lunch"], f"{CB_MT}{meal_id}:lunch")],
            [(labels["snack"], f"{CB_MT}{meal_id}:snack"), (labels["dinner"], f"{CB_MT}{meal_id}:dinner")],
        ]
    )


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
