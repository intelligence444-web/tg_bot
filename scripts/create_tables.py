"""Создание таблиц YDB.

    python -m scripts.create_tables              # создать таблицы (нужны YDB_ENDPOINT, YDB_DATABASE)
    python -m scripts.create_tables --print-yql  # только напечатать YQL, без подключения

Авторизация — как в боте (``bot.ydb_storage.connect``): например, ``YDB_ACCESS_TOKEN_CREDENTIALS=<IAM-токен>``
или ``YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS=<путь к ключу>``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ydb_storage import table_ddl  # noqa: E402


def yql(prefix: str = "") -> str:
    return "\n\n".join(table_ddl(prefix)) + "\n"


async def _create(prefix: str) -> None:
    from bot import ydb_storage  # noqa: PLC0415

    pool = await ydb_storage.connect(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"])
    try:
        await ydb_storage.create_tables(pool, prefix)
    finally:
        await ydb_storage.close(pool)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Создать таблицы бота в YDB (или напечатать YQL).")
    parser.add_argument("--print-yql", action="store_true", help="напечатать YQL и выйти, без подключения к базе")
    parser.add_argument("--prefix", default="", help="префикс имён таблиц (по умолчанию пустой)")
    args = parser.parse_args(argv)
    if args.print_yql:
        sys.stdout.write(yql(args.prefix))
        return 0
    missing = [name for name in ("YDB_ENDPOINT", "YDB_DATABASE") if not os.environ.get(name)]
    if missing:
        parser.error("не заданы переменные окружения: " + ", ".join(missing))
    asyncio.run(_create(args.prefix))
    print("Таблицы созданы (или уже были).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
