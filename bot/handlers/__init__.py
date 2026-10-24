"""Роутеры бота.

Порядок важен: ``start`` (команды и согласие) → ``consent_gate`` (всё остальное без согласия)
→ ``onboarding`` (шаги анкеты по состояниям) → сценарии следующих задач → ``fallback``.
Новые роутеры добавляются в ``_feature_routers`` — ``fallback`` всегда последний.
"""
from __future__ import annotations

from aiogram import Router

from bot.handlers import fallback, onboarding, start


def _feature_routers() -> list[Router]:
    return []


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(
        start.build_router(),
        start.build_consent_gate(),
        onboarding.build_router(),
        *_feature_routers(),
        fallback.build_router(),
    )
    return root
