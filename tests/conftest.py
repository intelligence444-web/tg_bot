import os

import pytest

from bot.storage import MemoryStorage


def _require_ydb() -> None:
    if not os.environ.get("YDB_ENDPOINT"):
        pytest.skip("YDB_ENDPOINT is not set")


@pytest.fixture(
    params=[
        "memory",
        pytest.param("ydb", marks=pytest.mark.ydb),
    ]
)
async def storage(request):
    if request.param == "memory":
        yield MemoryStorage()
        return
    _require_ydb()
    # Таблицы с уникальным префиксом создаются на тест и удаляются после него.
    from bot.ydb_storage import make_test_storage  # noqa: PLC0415

    async with make_test_storage() as ydb_storage:
        yield ydb_storage


@pytest.fixture
async def ydb_tables():
    """(pool, prefix) с временными таблицами; нужен YDB_ENDPOINT и YDB_DATABASE."""
    _require_ydb()
    from bot.ydb_storage import temp_tables  # noqa: PLC0415

    async with temp_tables() as pool_and_prefix:
        yield pool_and_prefix
