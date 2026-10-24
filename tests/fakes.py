from __future__ import annotations

from bot.models import Analysis


class FakeVision:
    """Отдаёт заранее заданные результаты по очереди и запоминает вызовы."""

    def __init__(self, results: list[Analysis | Exception]):
        self._results = list(results)
        self.calls: list[tuple] = []

    def _next(self) -> Analysis:
        r = self._results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def analyze(self, image_jpeg: bytes, caption: str | None) -> Analysis:
        self.calls.append(("analyze", image_jpeg, caption))
        return self._next()

    async def correct(self, previous: Analysis, text: str) -> Analysis:
        self.calls.append(("correct", previous, text))
        return self._next()
