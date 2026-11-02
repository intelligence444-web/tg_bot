from __future__ import annotations

import httpx
import pytest

from bot.speech import (
    METADATA_TOKEN_URL,
    SpeechError,
    YandexSpeech,
    metadata_token_provider,
)
from tests.fakes import FakeSpeech


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _tok() -> str:
    return "IAM-1"


async def test_recognize_request_and_result():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"result": "гречка с курицей"})

    async with _client(handler) as http:
        text = await YandexSpeech(http, _tok).recognize(b"OGG")
    assert text == "гречка с курицей"
    req = seen[0]
    assert req.method == "POST"
    assert req.url.host == "stt.api.cloud.yandex.net"
    assert req.url.path == "/speech/v1/stt:recognize"
    assert req.url.params["lang"] == "ru-RU"
    assert req.url.params["format"] == "oggopus"
    assert req.headers["Authorization"] == "Bearer IAM-1"
    assert req.content == b"OGG"


async def test_empty_result_is_empty_string():
    async with _client(lambda r: httpx.Response(200, json={"result": ""})) as http:
        assert await YandexSpeech(http, _tok).recognize(b"x") == ""


@pytest.mark.parametrize("status", [400, 500])
async def test_http_error(status):
    async with _client(lambda r: httpx.Response(status, json={"error_message": "x"})) as http:
        with pytest.raises(SpeechError):
            await YandexSpeech(http, _tok).recognize(b"x")


async def test_bad_body():
    async with _client(lambda r: httpx.Response(200, json={"nope": 1})) as http:
        with pytest.raises(SpeechError):
            await YandexSpeech(http, _tok).recognize(b"x")


async def test_network_error_wrapped():
    def handler(req):
        raise httpx.ConnectError("boom")

    async with _client(handler) as http:
        with pytest.raises(SpeechError):
            await YandexSpeech(http, _tok).recognize(b"x")


async def test_too_large_no_request():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"result": "x"})

    async with _client(handler) as http:
        with pytest.raises(SpeechError):
            await YandexSpeech(http, _tok).recognize(b"0" * (1024 * 1024 + 1))
        assert await YandexSpeech(http, _tok).recognize(b"0" * (1024 * 1024)) == "x"
    assert len(calls) == 1


async def test_token_provider_caches_and_refreshes():
    clock = [1000.0]
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        n = len(seen)
        return httpx.Response(
            200, json={"access_token": f"tok{n}", "expires_in": 600, "token_type": "Bearer"}
        )

    async with _client(handler) as http:
        get = metadata_token_provider(http, now=lambda: clock[0])
        assert await get() == "tok1"
        clock[0] += 500
        assert await get() == "tok1"  # 500 < 600 - 60
        assert len(seen) == 1
        clock[0] += 40  # 540 == expires_in - 60 -> обновить
        assert await get() == "tok2"
    assert str(seen[0].url) == METADATA_TOKEN_URL
    assert seen[0].headers["Metadata-Flavor"] == "Google"


async def test_token_provider_errors():
    async with _client(lambda r: httpx.Response(404)) as http:
        with pytest.raises(SpeechError):
            await metadata_token_provider(http)()
    async with _client(lambda r: httpx.Response(200, json={"x": 1})) as http:
        with pytest.raises(SpeechError):
            await metadata_token_provider(http)()


async def test_fake_speech():
    f = FakeSpeech(["a", SpeechError("x")])
    assert await f.recognize(b"1") == "a"
    with pytest.raises(SpeechError):
        await f.recognize(b"2")
    assert f.calls == [b"1", b"2"]
