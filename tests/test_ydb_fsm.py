import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

pytestmark = pytest.mark.ydb


class Form(StatesGroup):
    age = State()


@pytest.fixture
async def fsm(ydb_tables):
    from bot.ydb_storage import YdbFsmStorage  # noqa: PLC0415

    pool, prefix = ydb_tables
    storage = YdbFsmStorage(pool, prefix)
    yield storage
    await storage.close()


KEY = StorageKey(bot_id=1, chat_id=10, user_id=100)
OTHER = StorageKey(bot_id=1, chat_id=20, user_id=200)


async def test_state_set_get(fsm):
    assert await fsm.get_state(KEY) is None
    await fsm.set_state(KEY, Form.age)
    assert await fsm.get_state(KEY) == "Form:age"
    await fsm.set_state(KEY, "custom:step")
    assert await fsm.get_state(KEY) == "custom:step"
    assert await fsm.get_state(OTHER) is None


async def test_data_set_get(fsm):
    assert await fsm.get_data(KEY) == {}
    await fsm.set_data(KEY, {"age": 30, "items": [1, 2], "name": "Аня"})
    assert await fsm.get_data(KEY) == {"age": 30, "items": [1, 2], "name": "Аня"}
    await fsm.set_data(KEY, {"x": 1})
    assert await fsm.get_data(KEY) == {"x": 1}
    assert await fsm.get_data(OTHER) == {}


async def test_state_and_data_independent(fsm):
    await fsm.set_state(KEY, Form.age)
    await fsm.set_data(KEY, {"a": 1})
    assert await fsm.get_state(KEY) == "Form:age"
    await fsm.set_state(KEY, None)
    assert await fsm.get_state(KEY) is None
    assert await fsm.get_data(KEY) == {"a": 1}
    assert await fsm.update_data(KEY, {"b": 2}) == {"a": 1, "b": 2}
