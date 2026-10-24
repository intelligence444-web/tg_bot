"""Точки входа Cloud Functions (файл лежит в корне архива, как требует рантайм Python).

Функция bot: точка входа ``index.handler``; функция daily: ``index.daily``.
"""
from bot.logging_setup import setup_logging

setup_logging()

from bot.daily import handler as daily  # noqa: E402
from bot.main import handler  # noqa: E402

__all__ = ["handler", "daily"]
