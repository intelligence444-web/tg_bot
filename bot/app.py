"""Сборка диспетчера: зависимости и роутеры."""
from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage

from bot.handlers import build_router
from bot.storage import Storage
from bot.vision import VisionClient


@dataclass
class Deps:
    storage: Storage
    vision: VisionClient
    fetch_photo: Callable[[Bot, str], Awaitable[bytes]]
    clock: Callable[[], datetime]  # aware UTC
    admin_chat_id: int | None
    rng: random.Random


def build_dispatcher(deps: Deps, fsm_storage: BaseStorage) -> Dispatcher:
    """Диспетчер с роутерами ``build_router()``.

    Обработчики получают аргументами ``deps`` и каждое поле ``Deps`` по имени
    (``storage``, ``vision``, ``clock`` …). Поля, положенные по имени, — снимок на момент
    сборки; если поле подменяется позже (в тестах), читать его надо через ``deps``.
    """
    dp = Dispatcher(storage=fsm_storage)
    dp["deps"] = deps
    for f in fields(deps):
        dp[f.name] = getattr(deps, f.name)
    dp.include_router(build_router())
    return dp
