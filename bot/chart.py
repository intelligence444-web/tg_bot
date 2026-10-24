"""PNG-график веса (спец. 6.8)."""
from __future__ import annotations

import io
from datetime import date

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from bot.models import Weighing  # noqa: E402
from bot.progress import trend_series  # noqa: E402

LABEL_WEIGHT = "Взвешивания"
LABEL_TREND = "Тренд"
LABEL_TARGET = "Цель"
LABEL_FORECAST = "Прогноз"
LABEL_Y = "кг"


def render_weight_chart(
    weighings: list[Weighing],
    target_kg: float,
    forecast: tuple[date, float] | None,
) -> bytes:
    points = sorted(weighings, key=lambda w: w.day)
    trend = trend_series(weighings)
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=100)
    try:
        ax.scatter([w.day for w in points], [w.weight_kg for w in points],
                   color="#8fb8de", s=22, label=LABEL_WEIGHT, zorder=2)
        if trend:
            ax.plot([d for d, _ in trend], [v for _, v in trend],
                    color="#1f6fb2", linewidth=2, label=LABEL_TREND, zorder=3)
        ax.axhline(target_kg, color="#2e9e5b", linestyle="--", linewidth=1.5, label=LABEL_TARGET)
        if forecast is not None and trend:
            last_day, last_val = trend[-1]
            ax.plot([last_day, forecast[0]], [last_val, forecast[1]],
                    color="#1f6fb2", linestyle=":", linewidth=2, label=LABEL_FORECAST)
        ax.set_ylabel(LABEL_Y)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        return buf.getvalue()
    finally:
        plt.close(fig)
