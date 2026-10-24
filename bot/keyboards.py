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
