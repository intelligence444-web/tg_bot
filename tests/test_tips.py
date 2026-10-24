import random

import pytest

from bot import texts
from bot.models import Analysis, FoodItem
from bot.tips import Tip, choose_tip


def item(kcal=400, protein=30, category="main", name="x"):
    return FoodItem(name, 100, kcal, protein, 10, 30, False, category)


def an(*items):
    return Analysis(True, "high", tuple(items), False)


def tip(a, meal="lunch", before=0, norm=2000, sent=False, last=None, seed=1):
    return choose_tip(a, meal, before, norm, sent, last or {}, random.Random(seed))


VEG = item(50, 2, "vegetable")


def test_low_protein():
    t = tip(an(item(400, 10), VEG))  # 40/400 = 10%
    assert t.rule == "low_protein"
    assert t.text in texts.TIPS["low_protein"]


def test_drink():
    t = tip(an(item(400, 30), item(150, 20, "drink"), VEG))
    assert t.rule == "drink"
    assert "150" in t.text and "{kcal}" not in t.text


def test_drink_below_threshold():
    assert tip(an(item(400, 20), item(149, 10, "drink"), VEG)) is None


def test_sweet():
    t = tip(an(item(300, 30), item(100, 5, "sweet")), meal="snack")
    assert t.rule == "sweet"


def test_no_veg():
    assert tip(an(item(500, 30))).rule == "no_veg"
    assert tip(an(item(500, 30)), meal="dinner").rule == "no_veg"


@pytest.mark.parametrize("meal", ["breakfast", "snack"])
def test_no_veg_not_for_breakfast_snack(meal):
    assert tip(an(item(500, 30)), meal=meal) is None


def test_no_veg_needs_400_kcal():
    assert tip(an(item(399, 20))) is None


def test_high_protein():
    t = tip(an(item(250, 20)), meal="breakfast")  # 80/250 = 32%
    assert t.rule == "high_protein"


def test_high_protein_needs_200_kcal():
    assert tip(an(item(199, 20)), meal="breakfast") is None


def test_priority_low_protein_over_sweet():
    t = tip(an(item(400, 10), item(100, 0, "sweet")), meal="breakfast")
    assert t.rule == "low_protein"


def test_overeat_first_and_single_variant():
    t = tip(an(item(500, 30)), before=1800, norm=2000)
    assert t == Tip("overeat", 0, texts.OVEREAT)


def test_overeat_boundaries():
    a = an(item(500, 30))
    assert tip(a, before=1501, norm=2000).rule == "overeat"
    assert tip(a, before=2000, norm=2000).rule == "overeat"  # уже ровно в норме, приём перебил
    assert tip(a, before=1500, norm=2000).rule == "no_veg"  # ровно в норму — не перебор


def test_overeat_once_per_day():
    t = tip(an(item(500, 30)), before=1800, norm=2000, sent=True)
    assert t.rule == "no_veg"


def test_overeat_already_before_meal():
    t = tip(an(item(500, 30)), before=2100, norm=2000)
    assert t.rule == "no_veg"


def test_none_when_nothing_fits():
    assert tip(an(item(150, 12)), meal="snack") is None


def test_variant_not_repeated():
    a = an(item(300, 30), item(100, 5, "sweet"))
    seen = set()
    for s in range(100):
        t = tip(a, meal="snack", last={"sweet": 0}, seed=s)
        assert t.variant != 0
        seen.add(t.variant)
    assert seen == {1, 2}


def test_tips_texts_tone():
    banned = ("нельзя", "запрещено", "сорвался", "плохо")
    assert set(texts.TIPS) == {"low_protein", "drink", "sweet", "no_veg", "high_protein"}
    for variants in texts.TIPS.values():
        assert len(variants) == 3
        for v in variants:
            assert not any(b in v.lower() for b in banned)
            assert sum(1 for ch in v if ord(ch) > 0x2000) <= 2
    assert "{kcal}" in "".join(texts.TIPS["drink"])
