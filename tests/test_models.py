import pytest

from bot.models import Analysis, FoodItem


def item(name="Хлеб", grams=100, kcal=250, hidden=False, category="side", fat_type=None, fat=1.0):
    return FoodItem(name, grams, kcal, 5.0, fat, 40.0, hidden, category, fat_type)


def test_analysis_sums():
    a = Analysis(True, "high", (item(kcal=250), item(kcal=100, fat=2.0)), False)
    assert a.kcal == 350
    assert a.protein == 10
    assert a.fat == 3
    assert a.carbs == 80


def test_with_fat_butter():
    hidden = item("Масло", 5, 37, True, "fat", "сливочное")
    a = Analysis(True, "high", (item(), hidden), True)
    b = a.with_fat(10)
    fats = [i for i in b.items if i.category == "fat"]
    assert len(fats) == 1
    assert fats[0].grams == 10
    assert fats[0].kcal == pytest.approx(74.8)
    assert fats[0].fat == pytest.approx(8.25)
    assert b.ask_about_fat is False
    assert len(b.items) == 2


def test_with_fat_zero_removes_hidden():
    hidden = item("Масло", 5, 45, True, "fat", None)
    a = Analysis(True, "high", (item(), hidden), True)
    b = a.with_fat(0)
    assert b.items == (item(),)
    assert b.ask_about_fat is False


def test_with_fat_default_sunflower():
    a = Analysis(True, "high", (item(),), True)
    b = a.with_fat(20)
    fat = [i for i in b.items if i.category == "fat"][0]
    assert fat.kcal == pytest.approx(179.8)
    assert fat.fat == pytest.approx(19.98)


def test_with_fat_keeps_visible_fat():
    visible = item("Сыр", 30, 100, False, "fat")
    hidden = item("Масло", 5, 45, True, "fat")
    b = Analysis(True, "high", (visible, hidden), True).with_fat(5)
    assert visible in b.items
    assert len(b.items) == 2
