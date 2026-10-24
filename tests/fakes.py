from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    EditMessageText,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
)
from aiogram.types import User as TgUser

from bot.models import Analysis


class FakeVision:
    """Отдаёт заранее заданные результаты по очереди и запоминает вызовы."""

    def __init__(self, results: list[Analysis | Exception]):
        self._results = list(results)
        self.calls: list[tuple] = []

    def _next(self) -> Analysis:
        r = self._results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def analyze(self, image_jpeg: bytes, caption: str | None) -> Analysis:
        self.calls.append(("analyze", image_jpeg, caption))
        return self._next()

    async def correct(self, previous: Analysis, text: str) -> Analysis:
        self.calls.append(("correct", previous, text))
        return self._next()


# --- Telegram: подменная сессия aiogram, часы и конструкторы апдейтов ---

BOT_ID = 42
BOT_TOKEN = f"{BOT_ID}:TEST-TOKEN"
_MSG_DATE = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
_update_ids = itertools.count(1)
_incoming_ids = itertools.count(1_000_000)


class MockedSession(BaseSession):
    """Сессия без сети: запоминает запросы и отвечает правдоподобными объектами.

    - ``requests`` — все методы, вызванные ботом, по порядку;
    - ``SendMessage``/``SendPhoto``/``EditMessageText`` → ``Message`` (растущий ``message_id``,
      привязан к боту, так что у ответа работают ``.edit_text()`` и т. п.);
    - любой другой метод (``AnswerCallbackQuery``, ``EditMessageReplyMarkup`` …) → ``True``;
    - ``fail(method_type, exc, times=None)`` — бросать ``exc`` на этот тип метода
      (``times=None`` — всегда, иначе указанное число раз); запрос всё равно записывается.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._errors: dict[type, list[Any]] = {}
        self._message_ids = itertools.count(1)

    def fail(self, method_type: type, exc: Exception, times: int | None = None) -> None:
        self._errors[method_type] = [exc, times]

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        err = self._errors.get(type(method))
        if err is not None:
            exc, times = err
            if times is not None:
                err[1] = times - 1
                if err[1] <= 0:
                    del self._errors[type(method)]
            raise exc
        return self._respond(bot, method)

    def _respond(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, (SendMessage, SendPhoto, EditMessageText)):
            chat_id = method.chat_id
            chat = Chat(id=int(chat_id) if chat_id is not None else 0, type="private")
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            fields: dict[str, Any] = {
                "message_id": method.message_id if isinstance(method, EditMessageText) else next(self._message_ids),
                "date": _MSG_DATE,
                "chat": chat,
                "from_user": TgUser(id=bot.id, is_bot=True, first_name="Bot"),
                "reply_markup": markup,
            }
            if isinstance(method, SendPhoto):
                fields["photo"] = [PhotoSize(file_id="sent-photo", file_unique_id="sent-photo-u", width=1, height=1)]
                fields["caption"] = method.caption
            else:
                fields["text"] = method.text
            return Message(**fields).as_(bot)
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass

    # --- удобства для проверок ---
    def of_type(self, method_type: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, method_type)]

    def texts(self) -> list[str]:
        """Тексты всех SendMessage и EditMessageText по порядку."""
        return [r.text for r in self.requests if isinstance(r, (SendMessage, EditMessageText))]

    def last_message(self) -> SendMessage:
        return self.of_type(SendMessage)[-1]

    def clear(self) -> None:
        self.requests.clear()


class FixedClock:
    """Часы для ``Deps.clock``: всегда возвращают ``now`` (aware UTC)."""

    def __init__(self, dt: datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self.now = dt

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta

    def set(self, dt: datetime) -> None:
        self.now = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _tg_user(user_id: int) -> TgUser:
    return TgUser(id=user_id, is_bot=False, first_name="Тест", language_code="ru")


def make_message(
    text: str | None = None,
    photo: str | bool | None = None,
    document: str | None = None,
    media_group_id: str | None = None,
    user_id: int = 1,
    caption: str | None = None,
) -> Update:
    """Апдейт с входящим сообщением в личке ``user_id``.

    ``photo`` — file_id (или True); ``document`` — mime-type документа (``"image/jpeg"``).
    """
    fields: dict[str, Any] = {
        "message_id": next(_incoming_ids),
        "date": _MSG_DATE,
        "chat": Chat(id=user_id, type="private"),
        "from_user": _tg_user(user_id),
        "text": text,
        "caption": caption,
        "media_group_id": media_group_id,
    }
    if photo:
        file_id = photo if isinstance(photo, str) else "photo-file"
        fields["photo"] = [
            PhotoSize(file_id=f"{file_id}-small", file_unique_id=f"{file_id}-su", width=90, height=90),
            PhotoSize(file_id=file_id, file_unique_id=f"{file_id}-u", width=1280, height=960),
        ]
    if document:
        ext = document.rsplit("/", 1)[-1]
        fields["document"] = Document(
            file_id="doc-file", file_unique_id="doc-u", mime_type=document, file_name=f"file.{ext}"
        )
    return Update(update_id=next(_update_ids), message=Message(**fields))


def make_callback(data: str, user_id: int = 1, message_id: int = 1, message_text: str = "…") -> Update:
    """Апдейт с нажатием инлайн-кнопки на сообщении бота ``message_id``."""
    msg = Message(
        message_id=message_id,
        date=_MSG_DATE,
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="Bot"),
        text=message_text,
    )
    cq = CallbackQuery(
        id=str(next(_update_ids)), from_user=_tg_user(user_id), chat_instance="ci", data=data, message=msg
    )
    return Update(update_id=next(_update_ids), callback_query=cq)


def _key(user_id: int) -> StorageKey:
    return StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id)


def fsm_context(dp: Dispatcher, user_id: int = 1) -> FSMContext:
    """FSMContext личного чата ``user_id`` — для входа в сценарий из теста напрямую."""
    return FSMContext(storage=dp.fsm.storage, key=_key(user_id))


async def get_fsm_state(dp: Dispatcher, user_id: int = 1) -> str | None:
    return await dp.fsm.storage.get_state(_key(user_id))


async def get_fsm_data(dp: Dispatcher, user_id: int = 1) -> dict[str, Any]:
    return await dp.fsm.storage.get_data(_key(user_id))


async def onboard(
    dp: Dispatcher,
    bot: Bot,
    user_id: int = 1,
    *,
    sex: str = "female",
    age: str = "30",
    height: str = "165",
    weight: str = "70",
    activity: str = "moderate",
    goal: str = "lose",
    target: str | None = "62",
) -> None:
    """Проходит /start, согласие и анкету (по умолчанию — эталонный пример спец. 6.3)."""
    steps = [
        make_message("/start", user_id=user_id),
        make_callback("consent", user_id=user_id),
        make_callback(f"sex:{sex}", user_id=user_id),
        make_message(age, user_id=user_id),
        make_message(height, user_id=user_id),
        make_message(weight, user_id=user_id),
        make_callback(f"act:{activity}", user_id=user_id),
        make_callback(f"goal:{goal}", user_id=user_id),
    ]
    if goal != "maintain" and target is not None:
        steps.append(make_message(target, user_id=user_id))
    for upd in steps:
        await dp.feed_update(bot, upd)
