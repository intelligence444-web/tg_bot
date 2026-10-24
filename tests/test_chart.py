from datetime import date, timedelta

import matplotlib.pyplot as plt

from bot.chart import render_weight_chart
from bot.models import Weighing


def _ws(n):
    start = date(2026, 1, 1)
    return [Weighing(day=start + timedelta(days=i), weight_kg=90 - i * 0.1) for i in range(n)]


def test_two_points_no_forecast():
    png = render_weight_chart(_ws(2), 80.0, None)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 1000


def test_fifty_points_with_forecast():
    ws = _ws(50)
    png = render_weight_chart(ws, 80.0, (date(2026, 6, 1), 80.0))
    assert png.startswith(b"\x89PNG")
    assert len(png) > 1000


def test_figures_closed():
    before = len(plt.get_fignums())
    for _ in range(20):
        render_weight_chart(_ws(5), 80.0, (date(2026, 6, 1), 80.0))
    assert len(plt.get_fignums()) == before
