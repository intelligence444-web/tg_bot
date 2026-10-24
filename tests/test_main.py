"""Вебхук: секрет, отсечение повторов, base64, ошибки (спец. 13, 17)."""
from __future__ import annotations

import base64
import io
import json
import logging
from datetime import date, datetime, timezone

from PIL import Image

from bot.main import process_webhook
from bot.models import Analysis, FoodItem
from tests.fakes import FakeVision, make_message, onboard

SECRET = "s3cret"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 24)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


def _buckwheat() -> Analysis:
    return Analysis(True, "high", (FoodItem("Гречка", 150, 165, 6.0, 1.5, 33.0, False, "side"),), False)


def _event(update, secret: str | None = SECRET, b64: bool = False, header: str = "X-Telegram-Bot-Api-Secret-Token"):
    body = update if isinstance(update, str) else update.model_dump_json(exclude_none=True)
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[header] = secret
    if b64:
        body = base64.b64encode(body.encode()).decode()
    return {"httpMethod": "POST", "headers": headers, "body": body, "isBase64Encoded": b64}


async def _photo_env(bot_env, results):
    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    deps.vision = FakeVision(results)
    jpeg = _jpeg()

    async def fetch_photo(_bot, file_id: str) -> bytes:
        return jpeg

    deps.fetch_photo = fetch_photo
    session.clear()
    return dp, bot, session, deps


async def test_bad_secret_is_403_without_telegram_requests(bot_env):
    dp, bot, session, deps = bot_env
    upd = make_message("/start")
    for secret in ("wrong", None):
        resp = await process_webhook(_event(upd, secret=secret), dp, bot, deps.storage, SECRET, NOW)
        assert resp["statusCode"] == 403
    assert session.requests == []
    assert await deps.storage.get_user(1) is None


async def test_header_name_is_case_insensitive(bot_env):
    dp, bot, session, deps = bot_env
    upd = make_message("/start")
    resp = await process_webhook(
        _event(upd, header="x-telegram-bot-api-secret-token"), dp, bot, deps.storage, SECRET, NOW
    )
    assert resp["statusCode"] == 200
    assert len(session.requests) == 1


async def test_same_update_id_processed_once(bot_env):
    dp, bot, session, deps = await _photo_env(bot_env, [_buckwheat(), _buckwheat()])
    upd = make_message(photo="p1")
    for _ in range(2):
        resp = await process_webhook(_event(upd), dp, bot, deps.storage, SECRET, NOW)
        assert resp["statusCode"] == 200
    assert len(await deps.storage.meals_on(1, TODAY)) == 1
    assert len(deps.vision.calls) == 1


async def test_base64_body_is_decoded(bot_env):
    dp, bot, session, deps = await _photo_env(bot_env, [_buckwheat()])
    resp = await process_webhook(_event(make_message(photo="p1"), b64=True), dp, bot, deps.storage, SECRET, NOW)
    assert resp["statusCode"] == 200
    assert len(await deps.storage.meals_on(1, TODAY)) == 1


async def test_invalid_json_is_400(bot_env):
    dp, bot, session, deps = bot_env
    resp = await process_webhook(_event("{not json"), dp, bot, deps.storage, SECRET, NOW)
    assert resp["statusCode"] == 400
    assert session.requests == []


async def test_handler_exception_is_logged_and_200(bot_env, caplog):
    dp, bot, session, deps = await _photo_env(bot_env, [RuntimeError("boom")])
    with caplog.at_level(logging.ERROR, logger="bot.main"):
        resp = await process_webhook(_event(make_message(photo="p1")), dp, bot, deps.storage, SECRET, NOW)
    assert resp["statusCode"] == 200
    records = [r for r in caplog.records if r.name == "bot.main"]
    assert records and records[0].exc_info is not None
    assert getattr(records[0], "user_id", None) == 1
    assert getattr(records[0], "error_kind", None)


def test_event_body_roundtrip_is_json():
    upd = make_message("/start")
    assert json.loads(_event(upd)["body"])["update_id"] == upd.update_id
