"""Роутеры бота.

Порядок важен: ``start`` (команды и согласие) → ``consent_gate`` (всё остальное без согласия)
→ ``commands`` (/stop_tips, /start_tips, «❓ Помощь» — в любом состоянии) → ``onboarding`` (шаги анкеты по состояниям) → сценарии следующих задач → ``fallback``.
Новые роутеры добавляются в ``_feature_routers`` — ``fallback`` всегда последний.
"""
from __future__ import annotations

from aiogram import Router

from bot.handlers import diary, fallback, meals, onboarding, profile, start, weight


def _feature_routers() -> list[Router]:
    # weight — первым: сообщение в состоянии ввода веса, кроме числа, сбрасывает это состояние
    # (SkipHandler) и уходит дальше, в том числе к фото и кнопкам меню.
    return [weight.build_router(), meals.build_router(), diary.build_router(), profile.build_router()]


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(
        start.build_router(),
        start.build_consent_gate(),
        start.build_commands_router(),
        onboarding.build_router(),
        *_feature_routers(),
        fallback.build_router(),
    )
    return root
