"""Точки входа Cloud Functions (файл лежит в корне архива, как требует рантайм Python).

Функция bot: точка входа ``index.handler``; функция daily: ``index.daily``.
"""
from bot.daily import handler as daily
from bot.main import handler

__all__ = ["handler", "daily"]
