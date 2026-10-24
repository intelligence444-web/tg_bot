"""Фото еды, уточнение масла, правки записей и дневник (спец. 6.4, 6.5, 8.5, 10, 13)."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from PIL import Image

from bot import texts
from bot.handlers.meals import format_meal
from bot.models import Analysis, FoodItem, Meal, User
from bot.tips import Tip
from bot.vision import VisionUnavailable
from tests.fakes import FakeVision, make_callback, make_message, onboard

TODAY = date(2026, 10, 24)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


JPEG = _jpeg()


def item(name, grams, kcal, protein=0.0, fat=0.0, carbs=0.0, hidden=False, category="main", fat_type=None):
    return FoodItem(name, grams, kcal, protein, fat, carbs, hidden, category, fat_type)


def pasta(ask: bool = False, confidence: str = "high") -> Analysis:
    return Analysis(
        is_food=True,
        confidence=confidence,
        items=(
            item("Вермишель отварная", 200, 230, 7.0, 1.0, 46.0, category="side"),
            item("Котлета", 100, 220, 15.0, 15.0, 8.0, category="main"),
            item("Масло в гарнире", 5, 45, 0.0, 5.0, 0.0, hidden=True, category="fat"),
        ),
        ask_about_fat=ask,
    )


def buckwheat() -> Analysis:
    return Analysis(True, "high", (item("Гречка", 150, 165, 6.0, 1.5, 33.0, category="side"),), False)


def big(kcal: float) -> Analysis:
    return Analysis(True, "high", (item("Плов", 400, kcal, kcal * 0.2 / 4, 10.0, 50.0, category="main"),), False)


async def _setup(bot_env, results, onboarded=True):
    dp, bot, session, deps = bot_env
    deps.vision = FakeVision(results)

    async def fetch_photo(_bot, file_id: str) -> bytes:
        deps.fetched = file_id
        return JPEG

    deps.fetch_photo = fetch_photo
    if onboarded:
        await onboard(dp, bot)
    session.clear()
    return dp, bot, session, deps


def _answers(session) -> list[str | None]:
    return [r.text for r in session.of_type(AnswerCallbackQuery)]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- format_meal ---


def test_format_meal_lines():
    tip = Tip("sweet", 0, "Совет дня")
    text = format_meal(pasta(), "lunch", 1266.4, tip)
    lines = text.splitlines()
    assert "🍽 Вермишель отварная — 200 г — 230 ккал" in lines
    assert "🍽 Масло в гарнире — 5 г — 45 ккал (предположение)" in lines
    assert any(line.startswith("Итого") and "495 ккал" in line for line in lines)
    assert "✅ Записал в дневник: обед, 495 ккал" in lines
    assert any(line.startswith("Осталось на сегодня") and "1 266" in line for line in lines)
    assert lines[-1] == "Совет дня"
    assert "Не уверен" not in text


def test_format_meal_low_confidence():
    text = format_meal(pasta(confidence="low"), "dinner", 100, None)
    assert "Не уверен — проверь и поправь, если нужно" in text
    assert "ужин" in text


# --- фото ---


async def test_photo_at_13_records_lunch(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])
    deps.clock.set(datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc))
    await dp.feed_update(bot, make_message(photo="big", caption="без соли"))

    sends = session.of_type(SendMessage)
    assert sends[0].text == "Анализирую тарелку…"
    edits = session.of_type(EditMessageText)
    assert edits and edits[-1].message_id is not None
    result = edits[-1].text
    assert "Осталось на сегодня" in result
    assert deps.fetched == "big"
    assert deps.vision.calls[0][0] == "analyze" and deps.vision.calls[0][2] == "без соли"

    meals = await deps.storage.meals_on(1, TODAY)
    assert len(meals) == 1
    assert meals[0].meal_type == "lunch"
    assert meals[0].analysis.kcal == 495
    cbs = _callbacks(edits[-1].reply_markup)
    mid = meals[0].meal_id
    assert f"meal:edit:{mid}" in cbs and f"meal:del:{mid}" in cbs and f"meal:type:{mid}" in cbs
    assert not any(c.startswith("fat:") for c in cbs)


async def test_fat_buttons_little_recomputes(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta(ask=True)])
    await dp.feed_update(bot, make_message(photo=True))
    edit = session.of_type(EditMessageText)[-1]
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    cbs = _callbacks(edit.reply_markup)
    for v in ("none", "little", "much", "sauce"):
        assert f"fat:{meal.meal_id}:{v}" in cbs
    assert "Добавляли масло или соус?" in edit.text

    session.clear()
    await dp.feed_update(bot, make_callback(f"fat:{meal.meal_id}:little", message_id=edit.message_id))
    stored = await deps.storage.get_meal(1, meal.meal_id)
    oil = [i for i in stored.analysis.items if i.hidden and i.category == "fat"]
    assert len(oil) == 1 and oil[0].grams == 10
    assert stored.fat_answer == "little"
    assert round(stored.analysis.kcal, 1) == round(230 + 220 + 89.9, 1)
    new_edit = session.of_type(EditMessageText)[-1]
    assert new_edit.message_id == edit.message_id
    assert "Масло — 10 г — 90 ккал" in new_edit.text
    assert "Добавляли масло" not in new_edit.text
    cbs = _callbacks(new_edit.reply_markup)
    assert f"meal:edit:{meal.meal_id}" in cbs and not any(c.startswith("fat:") for c in cbs)


async def test_fat_none_removes_hidden_fat(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta(ask=True)])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"fat:{meal.meal_id}:none"))
    stored = await deps.storage.get_meal(1, meal.meal_id)
    assert stored.analysis.kcal == 450


async def test_fat_sauce_goes_to_correct(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta(ask=True), buckwheat()])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    session.clear()
    await dp.feed_update(bot, make_callback(f"fat:{meal.meal_id}:sauce"))
    assert any("соус" in t.lower() for t in session.texts())
    await dp.feed_update(bot, make_message("майонез, ложка"))
    call = deps.vision.calls[-1]
    assert call[0] == "correct" and call[1] == meal.analysis
    assert "майонез, ложка" in call[2]
    stored = await deps.storage.get_meal(1, meal.meal_id)
    assert stored.analysis == buckwheat()
    assert stored.meal_type == meal.meal_type and stored.eaten_at == meal.eaten_at
    assert "Гречка" in session.texts()[-1]


async def test_meal_edit_correction(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta(), buckwheat()])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"meal:edit:{meal.meal_id}"))
    session.clear()
    await dp.feed_update(bot, make_message("это гречка, 150 г"))
    assert deps.vision.calls[-1] == ("correct", meal.analysis, "это гречка, 150 г")
    stored = await deps.storage.get_meal(1, meal.meal_id)
    assert stored.analysis.kcal == 165
    assert "🍽 Гречка — 150 г — 165 ккал" in session.texts()[-1]
    # состояние сброшено: следующий текст — обычный fallback
    session.clear()
    await dp.feed_update(bot, make_message("привет"))
    assert session.texts()[-1] == texts.FALLBACK


async def test_meal_edit_vision_unavailable_keeps_record(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta(), VisionUnavailable("x")])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"meal:edit:{meal.meal_id}"))
    session.clear()
    await dp.feed_update(bot, make_message("это гречка, 150 г"))
    assert "Сервис распознавания перегружен" in session.texts()[-1]
    assert (await deps.storage.get_meal(1, meal.meal_id)).analysis == pasta()


async def test_meal_delete_and_stale(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    await dp.feed_update(bot, make_callback(f"meal:del:{meal.meal_id}"))
    assert await deps.storage.get_meal(1, meal.meal_id) is None
    assert await deps.storage.meals_on(1, TODAY) == []
    session.clear()
    await dp.feed_update(bot, make_callback(f"meal:del:{meal.meal_id}"))
    assert _answers(session) == ["Эта запись уже удалена"]
    session.clear()
    await dp.feed_update(bot, make_callback(f"fat:{meal.meal_id}:little"))
    assert _answers(session) == ["Эта запись уже удалена"]


async def test_meal_change_type(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])
    await dp.feed_update(bot, make_message(photo=True))
    meal = (await deps.storage.meals_on(1, TODAY))[0]
    assert meal.meal_type == "lunch"
    session.clear()
    await dp.feed_update(bot, make_callback(f"meal:type:{meal.meal_id}"))
    markups = [r.reply_markup for r in session.requests if getattr(r, "reply_markup", None) is not None]
    cbs = _callbacks(markups[-1])
    for t in ("breakfast", "lunch", "snack", "dinner"):
        assert f"mt:{meal.meal_id}:{t}" in cbs
    session.clear()
    await dp.feed_update(bot, make_callback(f"mt:{meal.meal_id}:dinner"))
    stored = await deps.storage.get_meal(1, meal.meal_id)
    assert stored.meal_type == "dinner"
    assert "Записал в дневник: ужин" in session.texts()[-1]


async def test_not_food(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [Analysis(False, "high", (), False)])
    await dp.feed_update(bot, make_message(photo=True))
    assert any("Не вижу еды" in t for t in session.texts())
    assert await deps.storage.meals_on(1, TODAY) == []


async def test_food_without_items_is_not_food(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [Analysis(True, "high", (), False)])
    await dp.feed_update(bot, make_message(photo=True))
    assert any("Не вижу еды" in t for t in session.texts())
    assert await deps.storage.meals_on(1, TODAY) == []


async def test_broken_image_is_not_food(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])

    async def fetch_photo(_bot, file_id):
        return b"not an image"

    deps.fetch_photo = fetch_photo
    await dp.feed_update(bot, make_message(photo=True))
    assert any("Не вижу еды" in t for t in session.texts())
    assert deps.vision.calls == []
    assert await deps.storage.meals_on(1, TODAY) == []


async def test_no_credits_alerts_admin_once_per_hour(bot_env):
    dp, bot, session, deps = await _setup(
        bot_env, [VisionUnavailable("x", no_credits=True), VisionUnavailable("x", no_credits=True)]
    )
    deps.admin_chat_id = 777
    await dp.feed_update(bot, make_message(photo=True))
    assert any("Сервис распознавания перегружен" in t for t in session.texts())
    admin = [r for r in session.of_type(SendMessage) if r.chat_id == 777]
    assert len(admin) == 1 and "Пополни баланс Kie" in admin[0].text
    assert await deps.storage.meals_on(1, TODAY) == []

    session.clear()
    deps.clock.advance(timedelta(minutes=10))
    await dp.feed_update(bot, make_message(photo=True))
    assert any("Сервис распознавания перегружен" in t for t in session.texts())
    assert [r for r in session.of_type(SendMessage) if r.chat_id == 777] == []


async def test_unavailable_without_no_credits_no_admin(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [VisionUnavailable("x")])
    deps.admin_chat_id = 777
    await dp.feed_update(bot, make_message(photo=True))
    assert any("Сервис распознавания перегружен" in t for t in session.texts())
    assert [r for r in session.of_type(SendMessage) if r.chat_id == 777] == []


async def test_image_document_is_processed(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])
    await dp.feed_update(bot, make_message(document="image/jpeg"))
    assert deps.fetched == "doc-file"
    assert len(await deps.storage.meals_on(1, TODAY)) == 1


async def test_pdf_document_fallback(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])
    await dp.feed_update(bot, make_message(document="application/pdf"))
    assert session.texts() == [texts.FALLBACK]
    assert deps.vision.calls == []


async def test_album_analyzed_once_with_one_hint(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta(), pasta(), pasta()])
    for _ in range(3):
        await dp.feed_update(bot, make_message(photo=True, media_group_id="g1"))
    assert len([c for c in deps.vision.calls if c[0] == "analyze"]) == 1
    hints = [t for t in session.texts() if t.startswith("Отправляй по одному фото")]
    assert len(hints) == 1
    assert len(await deps.storage.meals_on(1, TODAY)) == 1


async def test_photo_before_onboarding(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()], onboarded=False)
    await deps.storage.save_user(User(user_id=1, consent_at=deps.clock()))
    await dp.feed_update(bot, make_message(photo=True))
    t = session.texts()
    assert any(x.startswith("Сначала посчитаем твою норму") for x in t)
    assert t[-1] == texts.ASK_SEX
    assert deps.vision.calls == []


async def test_overeat_once_per_day(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [big(1000), big(1000), big(1000)])
    user = await deps.storage.get_user(1)
    assert 1000 < user.norm.kcal < 2000

    await dp.feed_update(bot, make_message(photo=True))
    assert texts.OVEREAT not in session.texts()[-1]
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    assert texts.OVEREAT in session.texts()[-1]
    assert (await deps.storage.get_user(1)).last_overeat_date == TODAY
    session.clear()
    await dp.feed_update(bot, make_message(photo=True))
    last = session.texts()[-1]
    assert texts.OVEREAT not in last
    assert any(v in last for v in texts.TIPS["no_veg"])


async def test_tip_variant_remembered(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [big(400)])
    await dp.feed_update(bot, make_message(photo=True))
    user = await deps.storage.get_user(1)
    assert "no_veg" in user.last_tip_variants


async def test_photo_after_midnight_msk_goes_to_new_day(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [pasta()])
    deps.clock.set(datetime(2026, 10, 24, 21, 30, tzinfo=timezone.utc))
    await dp.feed_update(bot, make_message(photo=True))
    assert await deps.storage.meals_on(1, TODAY) == []
    meals = await deps.storage.meals_on(1, date(2026, 10, 25))
    assert len(meals) == 1
    assert meals[0].meal_type == "snack"
    assert meals[0].local_date == date(2026, 10, 25)


async def test_today_diary_with_streak(bot_env):
    dp, bot, session, deps = await _setup(bot_env, [])
    yesterday_at = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
    await deps.storage.add_meal(Meal(1, "y1", yesterday_at, TODAY - timedelta(days=1), "lunch", pasta()))
    await deps.storage.add_meal(Meal(1, "t1", deps.clock(), TODAY, "lunch", pasta()))
    await dp.feed_update(bot, make_message(texts.BTN_TODAY))
    text = session.texts()[-1]
    assert text.startswith("📅 Сегодня — 495 / ")
    assert "🔥 2 дня подряд" in text
    assert "🍽 Обед — 495 ккал" in text
