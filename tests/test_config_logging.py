"""Логи одной строкой JSON, проверка настроек при старте, закреплённые зависимости."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# --- I7: формат логов ---


def _record(msg="vision failed", exc_info=None, **extra) -> logging.LogRecord:
    rec = logging.LogRecord("bot.handlers.meals", logging.WARNING, __file__, 1, msg, (), exc_info)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_includes_extra_fields():
    from bot.logging_setup import JsonFormatter

    line = JsonFormatter().format(_record(user_id=42, error_kind="no_credits"))
    assert "\n" not in line
    data = json.loads(line)
    assert data["level"] == "WARNING"
    assert data["logger"] == "bot.handlers.meals"
    assert data["message"] == "vision failed"
    assert data["user_id"] == 42
    assert data["error_kind"] == "no_credits"
    assert "exc_info" not in data


def test_json_formatter_exception_text_and_missing_extra():
    from bot.logging_setup import JsonFormatter

    try:
        raise ValueError("boom")
    except ValueError:
        rec = _record(msg="update failed", exc_info=sys.exc_info())
    data = json.loads(JsonFormatter().format(rec))
    assert data["user_id"] is None and data["error_kind"] is None
    assert "ValueError: boom" in data["exc_info"]


def test_setup_logging_once(monkeypatch):
    from bot import logging_setup

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    logging_setup.setup_logging()
    logging_setup.setup_logging()
    ours = [h for h in root.handlers if isinstance(h.formatter, logging_setup.JsonFormatter)]
    assert len(ours) == 1
    assert ours[0].stream is sys.stdout
    assert root.level == logging.INFO


def test_index_sets_up_logging():
    source = (ROOT / "index.py").read_text(encoding="utf-8")
    assert "setup_logging()" in source


async def test_chart_failure_logs_traceback(bot_env, monkeypatch, caplog):
    from datetime import timedelta

    from bot.handlers import weight
    from tests.fakes import make_callback, make_message, onboard

    dp, bot, session, deps = bot_env
    await onboard(dp, bot)
    deps.clock.advance(timedelta(days=1))
    await dp.feed_update(bot, make_message("⚖️ Взвеситься"))
    await dp.feed_update(bot, make_message("69"))

    def broken(*a, **kw):
        raise RuntimeError("render failed")

    monkeypatch.setattr(weight, "render_weight_chart", broken)
    with caplog.at_level(logging.WARNING):
        await dp.feed_update(bot, make_callback("chart"))
    recs = [r for r in caplog.records if getattr(r, "error_kind", None) == "chart"]
    assert recs and recs[0].exc_info is not None


# --- I9 / M9: настройки ---


async def test_init_runtime_without_privacy_url_fails_loudly(monkeypatch, caplog):
    from bot import main, texts

    monkeypatch.setattr(texts, "PRIVACY_URL", "")
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await main._init_runtime()
    assert any(getattr(r, "error_kind", None) == "config" and "PRIVACY_URL" in r.getMessage() for r in caplog.records)


def test_texts_have_no_placeholder_privacy_url():
    source = (ROOT / "bot" / "texts.py").read_text(encoding="utf-8")
    assert "example.com" not in source


@pytest.mark.parametrize("raw, expected", [("", None), ("  ", None), ("12345", 12345), ("-100500", -100500)])
def test_admin_chat_id_parse(raw, expected):
    from bot.main import parse_admin_chat_id

    assert parse_admin_chat_id(raw) == expected


def test_admin_chat_id_garbage_logs_config_error(caplog):
    from bot.main import parse_admin_chat_id

    with caplog.at_level(logging.ERROR):
        assert parse_admin_chat_id("@admin") is None
    assert any(getattr(r, "error_kind", None) == "config" for r in caplog.records)


# --- I8 / M8: зависимости и пример окружения ---


def test_requirements_pinned():
    lines = [x.strip() for x in (ROOT / "requirements.txt").read_text().splitlines() if x.strip() and not x.startswith("#")]
    assert {"aiogram==3.31.0", "httpx==0.28.1", "Pillow==13.0.0", "matplotlib==3.11.2", "ydb==3.33.2"} <= set(lines)
    assert any(x.startswith("tzdata==") for x in lines)
    assert all("==" in x for x in lines)
    assert "-r requirements.txt" in (ROOT / "requirements-dev.txt").read_text()


def test_env_example_names_index_handler():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "bot.main.handler" not in env and "index.handler" in env
