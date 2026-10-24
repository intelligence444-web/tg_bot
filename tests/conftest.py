import os

# Ссылка на политику обязательна в рабочем окружении (bot.main._check_config); тестам — своя.
os.environ.setdefault("PRIVACY_URL", "https://privacy.test/policy")

import pytest  # noqa: E402

from bot.storage import MemoryStorage  # noqa: E402


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


@pytest.fixture
async def bot_env():
    """(dp, bot, session, deps): диспетчер на MemoryStorage и FSM в памяти, без сети.

    Часы — ``FixedClock`` на 2026-10-24 09:00 UTC (12:00 МСК), ``vision`` — пустой ``FakeVision``,
    ``fetch_photo`` отдаёт ``b""``. Поля ``deps`` можно подменять в тесте: обработчики читают
    их из ``deps`` при каждом вызове.
    """
    import random  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    from aiogram import Bot  # noqa: PLC0415
    from aiogram.fsm.storage.memory import MemoryStorage as FsmMemoryStorage  # noqa: PLC0415

    from bot.app import Deps, build_dispatcher  # noqa: PLC0415
    from tests.fakes import BOT_TOKEN, FakeVision, FixedClock, MockedSession  # noqa: PLC0415

    async def fetch_photo(bot, file_id: str) -> bytes:
        return b""

    deps = Deps(
        storage=MemoryStorage(),
        vision=FakeVision([]),
        fetch_photo=fetch_photo,
        clock=FixedClock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)),
        admin_chat_id=None,
        rng=random.Random(0),
    )
    session = MockedSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = build_dispatcher(deps, FsmMemoryStorage())
    yield dp, bot, session, deps
    await bot.session.close()
