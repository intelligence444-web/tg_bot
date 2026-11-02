from datetime import date

from bot.limits import take_ai


async def test_take_ai(storage):
    d = date(2026, 11, 2)
    assert [await take_ai(storage, 1, d, 2) for _ in range(3)] == [True, True, False]
    assert await take_ai(storage, 1, date(2026, 11, 3), 2) is True
