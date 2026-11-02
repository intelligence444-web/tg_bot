from __future__ import annotations

import json
from datetime import datetime

import pytest

from bot import assistant, vision
from bot.assistant import (
    DayContext,
    Plan,
    PlanMeal,
    PlanOption,
    build_context_text,
    check_plan,
    format_plan,
    parse_routed,
    to_telegram_html,
)
from bot.models import Profile
from bot.vision import BadResponse

NOW = datetime(2026, 11, 2, 14, 5)
PROFILE = Profile("male", 35, 180, 86, "moderate", "lose", 78)


def _ctx(**kw) -> DayContext:
    base = dict(
        profile=PROFILE,
        trend_kg=85.2,
        week_change_kg=-0.6,
        target_kg=78,
        norm_kcal=2100.4,
        norm_protein=154.8,
        eaten=(1200, 80, 40, 120),
        meals=(("breakfast", 400), ("lunch", 800)),
        week_avg_kcal=1950.2,
        week_avg_protein=120.4,
        week_days_in_norm=4,
        now_local=NOW,
    )
    base.update(kw)
    return DayContext(**base)


# ---------- справка ----------

def test_context_with_profile():
    text = build_context_text(_ctx())
    assert "мужской" in text and "35 лет" in text and "180 см" in text
    assert "85,2 кг" in text  # вес по тренду
    assert "2 100 ккал" in text  # норма через format_num
    assert "155 г" in text  # белок нормы
    assert "Съедено: 1 200 ккал" in text
    assert "Осталось: 900 ккал" in text and "осталось" in text.lower()
    assert "завтрак — 400 ккал" in text and "обед — 800 ккал" in text
    assert "7 дней" in text and "1 950 ккал" in text and "120 г белка" in text
    assert "в норме ±10%: 4" in text
    assert "за неделю −0,6 кг" in text and "до цели 7,2 кг" in text
    assert "02.11.2026" in text and "14:05" in text and "Москв" in text
    assert "Профиля нет" not in text


def test_context_without_profile():
    text = build_context_text(
        _ctx(profile=None, trend_kg=None, week_change_kg=None, target_kg=None,
             norm_kcal=None, norm_protein=None, meals=(), week_avg_kcal=None,
             week_avg_protein=None, week_days_in_norm=0, eaten=(0, 0, 0, 0))
    )
    assert "Профиля нет" in text
    assert "Съедено: 0 ккал" in text
    assert "7 дней" in text
    assert "None" not in text


def test_context_over_norm():
    text = build_context_text(_ctx(eaten=(2300, 100, 80, 250)))
    assert "Осталось: 0 ккал" in text and "сверх нормы на 200 ккал" in text


# ---------- маршрутизатор ----------

FOOD_ANALYSIS = {
    "is_food": True, "confidence": "medium",
    "items": [{"name": "Яйцо варёное", "grams": 100, "kcal": 155, "protein": 12.6,
               "fat": 10.6, "carbs": 1.1, "hidden": False, "category": "main"}],
    "ask_about_fat": False, "comment": "Хороший белок",
}

GOOD_PLAN = {
    "kind": "plan",
    "meals": [
        {"type": "breakfast", "options": [
            {"text": "Омлет", "kcal": 500, "protein": 40, "fat": 20, "carbs": 40},
            {"text": "Творог с ягодами", "kcal": 400, "protein": 35, "fat": 10, "carbs": 42}]},
        {"type": "lunch", "options": [
            {"text": "Курица с рисом", "kcal": 700, "protein": 50, "fat": 20, "carbs": 80}]},
        {"type": "dinner", "options": [
            {"text": "Рыба с овощами", "kcal": 600, "protein": 45, "fat": 20, "carbs": 60}]},
        {"type": "snack", "options": [
            {"text": "Йогурт и яблоко", "kcal": 200, "protein": 15, "fat": 4, "carbs": 26}]},
    ],
    "notes": "Белок равномерно по приёмам пищи",
}


def test_routed_food_with_fence():
    raw = "```json\n" + json.dumps({"kind": "food", "analysis": FOOD_ANALYSIS}) + "\n```"
    r = parse_routed(raw)
    assert r.kind == "food"
    assert r.analysis is not None and r.analysis.items[0].name == "Яйцо варёное"
    assert r.analysis.comment == "Хороший белок"


def test_routed_food_bad_analysis():
    bad = dict(FOOD_ANALYSIS, confidence="huge")
    with pytest.raises(BadResponse):
        parse_routed(json.dumps({"kind": "food", "analysis": bad}))
    with pytest.raises(BadResponse):
        parse_routed(json.dumps({"kind": "food"}))


def test_routed_activity():
    r = parse_routed(json.dumps({"kind": "activity", "name": " бег ", "minutes": 30, "met": 8.0}))
    assert (r.kind, r.activity_name, r.minutes, r.met) == ("activity", "бег", 30.0, 8.0)


@pytest.mark.parametrize("minutes,met", [(0, 8), (601, 8), (30, 0.5), (30, 21), ("30", 8), (True, 8)])
def test_routed_activity_out_of_range(minutes, met):
    with pytest.raises(BadResponse):
        parse_routed(json.dumps({"kind": "activity", "name": "бег", "minutes": minutes, "met": met}))


def test_routed_activity_bounds_ok():
    assert parse_routed(json.dumps({"kind": "activity", "name": "x", "minutes": 1, "met": 1})).minutes == 1
    assert parse_routed(json.dumps({"kind": "activity", "name": "x", "minutes": 600, "met": 20})).met == 20


def test_routed_activity_empty_name():
    with pytest.raises(BadResponse):
        parse_routed(json.dumps({"kind": "activity", "name": " ", "minutes": 30, "met": 8}))


def test_routed_plan():
    r = parse_routed(json.dumps(GOOD_PLAN))
    assert r.kind == "plan" and r.plan is not None
    assert [m.type for m in r.plan.meals] == ["breakfast", "lunch", "dinner", "snack"]
    assert r.plan.meals[0].options[1] == PlanOption("Творог с ягодами", 400, 35, 10, 42)
    assert r.plan.notes == "Белок равномерно по приёмам пищи"


def _plan_with(**kw):
    d = json.loads(json.dumps(GOOD_PLAN))
    d.update(kw)
    return d


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(meals=d["meals"][:2]),  # 2 приёма
    lambda d: d["meals"].append(d["meals"][0]),  # 5 приёмов
    lambda d: d["meals"][0].update(options=[]),  # 0 вариантов
    lambda d: d["meals"][0].update(options=d["meals"][0]["options"] * 2),  # 4 варианта
    lambda d: d["meals"][0].update(type="brunch"),
    lambda d: d["meals"][0]["options"][0].update(kcal=-1),
    lambda d: d["meals"][0]["options"][0].update(text=""),
    lambda d: d["meals"][0]["options"][0].pop("protein"),
    lambda d: d.update(meals="нет"),
])
def test_routed_plan_invalid(mutate):
    d = _plan_with()
    mutate(d)
    with pytest.raises(BadResponse):
        parse_routed(json.dumps(d))


def test_routed_plan_notes_optional():
    d = _plan_with()
    del d["notes"]
    assert parse_routed(json.dumps(d)).plan.notes == ""


@pytest.mark.parametrize("kind", ["chat", "unclear"])
def test_routed_reply(kind):
    r = parse_routed(json.dumps({"kind": kind, "reply": "  Привет  "}))
    assert r.kind == kind and r.reply == "Привет"


@pytest.mark.parametrize("raw", [
    "не json",
    "",
    "[1, 2]",
    json.dumps({"kind": "joke", "reply": "x"}),
    json.dumps({"reply": "x"}),
    json.dumps({"kind": "chat", "reply": "   "}),
    json.dumps({"kind": "unclear"}),
    json.dumps({"kind": "chat", "reply": 5}),
])
def test_routed_garbage(raw):
    with pytest.raises(BadResponse):
        parse_routed(raw)


# ---------- проверка рациона ----------

def _plan(*first: tuple[float, float, float, float]) -> Plan:
    types = ("breakfast", "lunch", "dinner", "snack")
    return Plan(
        tuple(PlanMeal(types[i], (PlanOption(f"Блюдо {i}", *v),)) for i, v in enumerate(first)),
        "",
    )


# 4Б + 4У + 9Ж: 500 = 4*40 + 4*40 + 9*20 → 160+160+180 = 500
IDEAL = _plan((500, 40, 20, 40), (700, 50, 20, 80), (600, 45, 20, 60), (200, 15, 4, 26))


def test_check_plan_ideal():
    ok, text, dev = check_plan(IDEAL, 2000, 150)
    assert ok and text == "" and dev == pytest.approx(0)


def test_check_plan_kcal_low():
    plan = _plan((425, 40, 15, 40), (595, 50, 15, 60), (510, 45, 15, 40), (170, 15, 2, 20))
    ok, text, dev = check_plan(plan, 2000, 150)
    assert not ok and "ккал" in text
    assert dev == pytest.approx(-15)


def test_check_plan_kcal_high_sign():
    ok, _, dev = check_plan(IDEAL, 1700, 150)
    assert not ok and dev > 10


def test_check_plan_protein_low():
    ok, text, dev = check_plan(IDEAL, 2000, 150 / 0.8)  # белок 150 из 187,5 = 80%
    assert not ok and "белок" in text.lower()
    assert dev == pytest.approx(0)


def test_check_plan_protein_90_ok():
    ok, _, _ = check_plan(IDEAL, 2000, 150 / 0.9)
    assert ok


def test_check_plan_option_macros_mismatch():
    bad = Plan(
        IDEAL.meals[:1]
        + (PlanMeal("lunch", (IDEAL.meals[1].options[0],
                              PlanOption("Паста", 900, 20, 10, 60))),)
        + IDEAL.meals[2:],
        "",
    )
    ok, text, _ = check_plan(bad, 2000, 150)
    assert not ok and "Паста" in text


def test_check_plan_option_macros_within_15():
    near = _plan((570, 40, 20, 40), (700, 50, 20, 80), (600, 45, 20, 60), (200, 15, 4, 26))
    ok, _, _ = check_plan(near, 2070, 150)  # 500 по БЖУ vs 570: 12%
    assert ok


# ---------- форматирование рациона ----------

def test_format_plan():
    plan = parse_routed(json.dumps(GOOD_PLAN)).plan
    text = format_plan(plan, None)
    assert "Завтрак (~500 ккал, ~40 г белка)" in text
    assert "Обед (~700 ккал, ~50 г белка)" in text
    assert "Ужин" in text and "Перекус" in text
    assert "1. Омлет — 500 ккал, Б 40 / Ж 20 / У 40 г" in text
    assert "2. Творог с ягодами — 400 ккал" in text
    assert "2 000 ккал" in text  # итог по первым вариантам
    assert "Белок равномерно" in text
    assert "Суммы примерные" not in text


def test_format_plan_deviation_and_escape():
    plan = Plan((PlanMeal("breakfast", (PlanOption("Сыр <фета> & хлеб", 300, 15, 15, 25),)),)
                * 3, "<b>notes</b>")
    text = format_plan(plan, -12.4)
    assert "Суммы примерные: ±12% от нормы" in text
    assert "Сыр &lt;фета&gt; &amp; хлеб" in text
    assert "&lt;b&gt;notes&lt;/b&gt;" in text


# ---------- HTML для Telegram ----------

def test_html_escape_and_bold():
    assert to_telegram_html("a < b & **жир**") == ["a &lt; b &amp; <b>жир</b>"]


def test_html_strips_markdown():
    out = to_telegram_html("## Заголовок\n__курсив__ и `код`\n* пункт")
    assert out == ["Заголовок\nкурсив и код\n- пункт"]


def test_html_unpaired_bold_removed():
    assert to_telegram_html("**жир и белок") == ["жир и белок"]


def test_html_split_paragraphs():
    paras = [f"Абзац {i}. " + "слово " * 150 for i in range(10)]
    text = "\n\n".join(p.strip() for p in paras)
    assert len(text) > 9000
    parts = to_telegram_html(text)
    assert len(parts) >= 3
    assert all(len(p) <= 4000 for p in parts)
    assert "\n\n".join(parts) == text


def test_html_split_long_paragraph_by_sentences():
    text = " ".join(f"Предложение номер {i} про белок & жир." for i in range(400))
    parts = to_telegram_html(text)
    assert len(parts) >= 3
    assert all(len(p) <= 4000 for p in parts)
    assert " ".join(parts) == text.replace("&", "&amp;")


def test_html_split_hard_no_spaces():
    text = "я" * 9000
    parts = to_telegram_html(text)
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == text


def test_html_split_never_breaks_entities():
    text = "&" * 5000
    parts = to_telegram_html(text)
    assert all(len(p) <= 4000 for p in parts)
    assert all(p.startswith("&amp;") and p.endswith("&amp;") for p in parts)
    assert "".join(parts) == "&amp;" * 5000


def test_html_empty():
    assert to_telegram_html("  ") == []


# ---------- системная инструкция ----------

def test_system_prompt_rules():
    p = assistant.SYSTEM_PROMPT
    # тон и посыл
    assert "без запретов и без голода" in p
    assert "на «ты»" in p
    # числа только из справки
    assert "только из справки" in p and "не пересчитывай" in p
    # медицина
    for word in ("диагноз", "лекарств", "БАД", "дозировк", "беременност", "кормлени",
                 "хронически", "меньше 18 лет", "врач"):
        assert word in p, word
    # РПП
    for word in ("голодовк", "рвот", "наказани", "страх", "специалист", "дефицит"):
        assert word in p, word
    # вне темы
    assert "одной фразой" in p
    # формат ответа
    assert "1200" in p and "без Markdown" in p
    assert "Отвечай строго одним JSON-объектом" in p


def test_system_prompt_kinds():
    p = assistant.SYSTEM_PROMPT
    for kind in ("food", "activity", "plan", "chat", "unclear"):
        assert f'"kind": "{kind}"' in p, kind
    # когда выбирать каждый вид
    assert "съел или выпил" in p
    assert "тренировк" in p
    assert "меню или рацион" in p
    assert "уточняющий вопрос" in p
    assert '"minutes"' in p and '"met"' in p
    assert "1–600" in p and "1–20" in p


def test_system_prompt_plan_rules():
    p = assistant.SYSTEM_PROMPT
    assert "3–4" in p and "1–3" in p
    for t in ("breakfast", "lunch", "dinner", "snack"):
        assert t in p
    assert "kcal ≈ 4·protein + 4·carbs + 9·fat" in p
    assert "исключения" in p
    assert "±10%" in p and "90%" in p


# ---------- правки ревью (раунд 1) ----------

def test_system_prompt_general_safety_applies_to_all_kinds():
    p = assistant.SYSTEM_PROMPT
    assert "ОБЩИЕ ПРАВИЛА — для любого вида ответа" in p
    assert '"notes" рациона' in p and '"comment" к еде' in p
    assert "ни в ответе, ни в рационе, ни в комментарии" in p
    # плановые/дефицитные запросы при медицинских случаях и РПП → chat
    assert "просит рацион или дефицит калорий" in p
    assert 'рацион не составляй: выбери "chat", ответь бережно' in p
    # рацион без профиля → chat с предложением заполнить профиль
    assert "«Профиля нет» или нет нормы" in p
    assert "одной строкой предложи сначала заполнить профиль" in p
    # общие правила идут до выбора вида
    assert p.index("ОБЩИЕ ПРАВИЛА") < p.index("КАК ВЫБРАТЬ ВИД ОТВЕТА")


def test_system_prompt_latest_context_only():
    assert "используй только последнюю справку" in assistant.SYSTEM_PROMPT


def test_system_prompt_examples_parse():
    p = assistant.SYSTEM_PROMPT
    assert '"high | medium | low"' not in p
    assert vision.ANALYSIS_JSON.replace('"high | medium | low"', '"medium"') in p
    examples = [line for line in p.splitlines() if line.startswith('{"kind"')]
    kinds = {parse_routed(line).kind for line in examples}
    assert kinds == {"food", "activity", "plan", "chat", "unclear"}
    food = parse_routed(assistant.FOOD_EXAMPLE)
    assert food.analysis.confidence == "medium" and food.analysis.items


def test_system_prompt_limits_from_vision():
    p = assistant.SYSTEM_PROMPT
    assert f"Граммы не больше {vision.MAX_GRAMS}" in p
    assert f"не больше {vision.MAX_KCAL}" in p


def test_routed_extracts_object_from_text():
    raw = 'Вот ответ: {"kind": "chat", "reply": "Привет"} — надеюсь, помог'
    assert parse_routed(raw).reply == "Привет"
    with pytest.raises(BadResponse):
        parse_routed("текст { не json }")
    with pytest.raises(BadResponse):
        parse_routed("} наоборот {")


def test_context_weight_label_without_trend():
    text = build_context_text(_ctx(trend_kg=None, week_change_kg=None))
    assert "вес 86 кг" in text and "по тренду" not in text
    assert "вес по тренду 85,2 кг" in build_context_text(_ctx())


def test_format_plan_near_norm_without_percent():
    plan = parse_routed(json.dumps(GOOD_PLAN)).plan
    text = format_plan(plan, 0.0)
    assert "Суммы примерные" in text and "±0%" not in text and "%" not in text
    text = format_plan(plan, 9.6)
    assert "Суммы примерные" in text and "±10%" not in text
    assert "±11% от нормы" in format_plan(plan, -10.6)


def test_html_split_long_paragraph_by_lines():
    lines = [f"- пункт {i}: " + "белок " * 20 for i in range(80)]
    text = "\n".join(line.strip() for line in lines)
    assert len(text) > 8000
    parts = to_telegram_html(text)
    assert len(parts) >= 2 and all(len(p) <= 4000 for p in parts)
    assert "\n".join(parts) == text
    assert all(p.startswith("- пункт") for p in parts)


def test_html_italic_triple_bold_links():
    assert to_telegram_html("*мягко* и ***важно***") == ["мягко и <b>важно</b>"]
    assert to_telegram_html("2 * 3 * 4") == ["2 * 3 * 4"]
    assert to_telegram_html("см. [статью](https://example.com/a_b) & **итог**") == [
        "см. статью &amp; <b>итог</b>"
    ]
