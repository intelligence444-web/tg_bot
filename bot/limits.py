from __future__ import annotations

from datetime import date

from bot.storage import Storage


async def take_ai(storage: Storage, user_id: int, day: date, limit: int) -> bool:
    """Учитывает одно обращение к ИИ; True, если оно укладывается в дневной лимит."""
    return await storage.incr_ai_usage(user_id, day) <= limit
