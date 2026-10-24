"""Привязка вебхука Telegram к API Gateway.

    BOT_TOKEN=... WEBHOOK_SECRET=... python scripts/set_webhook.py https://<id>.apigw.yandexcloud.net/webhook

Если адрес передан без ``/webhook`` на конце, он будет добавлен.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

ALLOWED_UPDATES = ["message", "callback_query"]


def webhook_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/webhook") else url + "/webhook"


def build_params(url: str, secret: str) -> dict[str, Any]:
    return {
        "url": webhook_url(url),
        "secret_token": secret,
        "allowed_updates": ALLOWED_UPDATES,
        "drop_pending_updates": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Вызвать setWebhook: secret_token=WEBHOOK_SECRET, allowed_updates=message,callback_query, "
        "drop_pending_updates=true. Нужны переменные BOT_TOKEN и WEBHOOK_SECRET."
    )
    parser.add_argument("url", help="адрес API Gateway, например https://d5d....apigw.yandexcloud.net/webhook")
    args = parser.parse_args(argv)
    missing = [name for name in ("BOT_TOKEN", "WEBHOOK_SECRET") if not os.environ.get(name)]
    if missing:
        parser.error("не заданы переменные окружения: " + ", ".join(missing))

    import httpx  # noqa: PLC0415

    params = build_params(args.url, os.environ["WEBHOOK_SECRET"])
    resp = httpx.post(f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/setWebhook", json=params, timeout=30)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"ok": False, "description": f"HTTP {resp.status_code}"}
    print(json.dumps(payload, ensure_ascii=False))
    if payload.get("ok"):
        print(f"Вебхук установлен: {params['url']}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
