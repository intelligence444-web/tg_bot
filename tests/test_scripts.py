"""Скрипты деплоя и документация: без сети и без YDB."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from bot.models import Analysis, FoodItem
from bot.ydb_storage import table_ddl
from scripts import create_tables, eval_vision, set_webhook

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_print_yql_equals_ddl():
    proc = _run("-m", "scripts.create_tables", "--print-yql")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "\n\n".join(table_ddl()) + "\n"
    assert proc.stdout == create_tables.yql()


def test_deploy_md_contains_every_create_table_verbatim():
    statements = table_ddl()
    assert len(statements) == 6
    for statement in statements:
        assert statement in DEPLOY
    assert "transition_from_kcal Double" in DEPLOY


def test_deploy_md_contains_api_gateway_spec():
    spec = (ROOT / "docs" / "api-gateway.yaml").read_text(encoding="utf-8")
    assert spec in DEPLOY
    assert "type: cloud_functions" in spec and "/webhook:" in spec and "post:" in spec


def test_deploy_md_key_values():
    for needle in (
        "index.handler", "index.daily", "python312", "0 6 ? * * *",
        "ydb.editor", "functions.functionInvoker", "serverless.functions.invoker",
        "YDB_METADATA_CREDENTIALS", "%5B%22message%22%2C%22callback_query%22%5D",
        "drop_pending_updates=true", "ZIP-архив",
    ):
        assert needle in DEPLOY, needle


def test_env_example_lists_every_env_var_read_by_code():
    names: set[str] = set()
    for path in ("bot/main.py", "bot/daily.py", "bot/texts.py", "bot/ydb_storage.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        names |= set(re.findall(r"os\.environ(?:\.get\()?\[?\(?[\"']([A-Z_]+)[\"']", src))
    assert {"BOT_TOKEN", "WEBHOOK_SECRET", "YDB_ENDPOINT", "VISION_PRIMARY"} <= names
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = dict(re.findall(r"^([A-Z_]+)=(.*)$", example, re.M))
    assert names <= set(declared), names - set(declared)
    assert all(v == "" for v in declared.values())


def test_help_works_offline():
    for script in ("scripts/eval_vision.py", "scripts/set_webhook.py", "scripts/create_tables.py"):
        proc = _run(script, "--help")
        assert proc.returncode == 0, (script, proc.stderr)
        assert "usage" in proc.stdout


def test_set_webhook_params():
    params = set_webhook.build_params("https://d5d.apigw.yandexcloud.net/", "sec")
    assert params == {
        "url": "https://d5d.apigw.yandexcloud.net/webhook",
        "secret_token": "sec",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    assert set_webhook.webhook_url("https://x.net/webhook") == "https://x.net/webhook"


def _analysis(kcal: float, is_food: bool = True) -> Analysis:
    return Analysis(is_food, "high", (FoodItem("Блюдо", 100, kcal, 1, 1, 1, False, "main"),), False)


def test_load_samples_and_evaluate(tmp_path):
    for name, kcal in (("a", 500), ("b", 200), ("c", 100)):
        (tmp_path / f"{name}.jpg").write_bytes(b"x")
        (tmp_path / f"{name}.json").write_text(json.dumps({"kcal": kcal, "protein": 1, "fat": 1, "carbs": 1}))
    (tmp_path / "orphan.jpg").write_bytes(b"x")
    samples = eval_vision.load_samples(tmp_path)
    assert [s.name for s in samples] == ["a", "b", "c"]

    answers = {b"a": _analysis(400), b"b": _analysis(250), b"c": None}

    async def analyze(img: bytes) -> Analysis:
        result = answers[img]
        if result is None:
            raise RuntimeError("boom")
        return result

    import asyncio

    images = {s.name: s.name.encode() for s in samples}
    r = asyncio.run(eval_vision.evaluate_model("openai:m", analyze, samples, images))
    assert r.failures == 1
    assert r.mae == 75  # (100 + 50) / 2
    assert r.mape == 22.5  # (20 + 25) / 2
    table = eval_vision.format_table([r], len(samples))
    assert "openai:m" in table and "75" in table and "22.5" in table and "1/3" in table


def test_eval_not_food_counts_as_failure(tmp_path):
    import asyncio

    s = eval_vision.Sample("x", tmp_path / "x.jpg", 300)

    async def analyze(img: bytes) -> Analysis:
        return _analysis(0, is_food=False)

    r = asyncio.run(eval_vision.evaluate_model("m", analyze, [s], {"x": b""}))
    assert r.failures == 1 and r.mae is None
    assert "—" in eval_vision.format_table([r], 1)


def test_index_shim_exposes_entry_points():
    import inspect

    import index
    from bot import daily, main

    assert index.handler is main.handler
    assert index.daily is daily.handler
    assert inspect.iscoroutinefunction(index.handler) and inspect.iscoroutinefunction(index.daily)
    assert set(index.__all__) == {"handler", "daily"}


def test_build_zip_includes_index_shim():
    script = (ROOT / "scripts" / "build_zip.sh").read_text(encoding="utf-8")
    assert "dist/bot.zip index.py bot requirements.txt" in script
    assert "`index.py`" in DEPLOY and "bot.main.handler" not in DEPLOY and "bot.daily.handler" not in DEPLOY
