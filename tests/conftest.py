import os

import pytest

from bot.storage import MemoryStorage


@pytest.fixture(
    params=[
        "memory",
        pytest.param("ydb", marks=pytest.mark.ydb),
    ]
)
def storage(request):
    if request.param == "memory":
        return MemoryStorage()
    if not os.environ.get("YDB_ENDPOINT"):
        pytest.skip("YDB_ENDPOINT is not set")
    # Hook for Task 7: the ydb branch lives in bot.ydb_storage (imported lazily).
    from bot.ydb_storage import make_test_storage  # noqa: PLC0415

    return make_test_storage()
