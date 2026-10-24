"""Сборка диспетчера: зависимости и роутеры."""
from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import TelegramObject

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


class ReactivateMiddleware(BaseMiddleware):
    """Любое сообщение или нажатие от неактивного пользователя снова делает его активным (спец. 13).

    Пользователей не создаёт: незнакомый пользователь проходит мимо.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        deps: Deps | None = data.get("deps")
        if from_user is not None and deps is not None:
            user = await deps.storage.get_user(from_user.id)
            if user is not None and not user.is_active:
                user.is_active = True
                await deps.storage.save_user(user)
        return await handler(event, data)


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
    reactivate = ReactivateMiddleware()
    dp.message.outer_middleware(reactivate)
    dp.callback_query.outer_middleware(reactivate)
    dp.include_router(build_router())
    return dp
