"""Распознавание голоса: Yandex SpeechKit (синхронный STT v1) и IAM-токен из метаданных ВМ."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)
MAX_AUDIO_BYTES = 1024 * 1024
STT_TIMEOUT = 20.0
TOKEN_MARGIN = 60.0


class SpeechError(Exception):
    """Не удалось распознать речь."""


class SpeechClient(Protocol):
    async def recognize(self, ogg_opus: bytes) -> str: ...


class YandexSpeech:
    def __init__(self, http: httpx.AsyncClient, token_provider: Callable[[], Awaitable[str]]):
        self._http = http
        self._token = token_provider

    async def recognize(self, ogg_opus: bytes) -> str:
        if len(ogg_opus) > MAX_AUDIO_BYTES:
            raise SpeechError("audio too large")
        try:
            token = await self._token()
            resp = await self._http.post(
                STT_URL,
                params={"lang": "ru-RU", "format": "oggopus"},
                headers={"Authorization": f"Bearer {token}"},
                content=ogg_opus,
                timeout=STT_TIMEOUT,
            )
        except SpeechError:
            raise
        except Exception as e:
            raise SpeechError(f"request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise SpeechError(f"stt status {resp.status_code}")
        try:
            result = resp.json()["result"]
        except Exception as e:
            raise SpeechError("bad stt response") from e
        if not isinstance(result, str):
            raise SpeechError("bad stt response")
        return result


def metadata_token_provider(
    http: httpx.AsyncClient, now: Callable[[], float] = time.monotonic
) -> Callable[[], Awaitable[str]]:
    """IAM-токен сервисного аккаунта ВМ; кэшируется до ``expires_in − 60`` с."""
    cached: dict[str, object] = {"token": None, "until": 0.0}

    async def get() -> str:
        token = cached["token"]
        if isinstance(token, str) and now() < float(cached["until"]):  # type: ignore[arg-type]
            return token
        try:
            resp = await http.get(
                METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5.0
            )
            if resp.status_code != 200:
                raise SpeechError(f"metadata status {resp.status_code}")
            data = resp.json()
            new_token = data["access_token"]
            expires_in = float(data["expires_in"])
            if not isinstance(new_token, str) or not new_token:
                raise ValueError("empty token")
        except SpeechError:
            raise
        except Exception as e:
            raise SpeechError(f"token fetch failed: {type(e).__name__}") from e
        cached["token"] = new_token
        cached["until"] = now() + expires_in - TOKEN_MARGIN
        return new_token

    return get
