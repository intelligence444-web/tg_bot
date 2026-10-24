from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
import warnings
from dataclasses import asdict
from typing import Any, Protocol

import httpx
from PIL import Image, ImageOps

from bot.models import Analysis, FoodItem

log = logging.getLogger(__name__)

CATEGORIES = ("main", "side", "vegetable", "fruit", "sweet", "drink", "sauce", "fat", "other")
_CONFIDENCE = ("high", "medium", "low")
MAX_GRAMS = 3000
MAX_KCAL = 5000
TIMEOUT_S = 45.0
MAX_SIDE = 1280
MAX_PIXELS = 40_000_000  # больше — считаем «бомбой» и не разжимаем


class VisionError(Exception):
    pass


class BadResponse(VisionError):
    pass


class NoCredits(VisionError):
    pass


class BadImage(ValueError):
    """Файл не картинка, битый или слишком большой по числу пикселей."""


class VisionUnavailable(VisionError):
    def __init__(self, message: str = "", no_credits: bool = False):
        super().__init__(message)
        self.no_credits = no_credits


class VisionClient(Protocol):
    async def analyze(self, image_jpeg: bytes, caption: str | None) -> Analysis: ...

    async def correct(self, previous: Analysis, text: str) -> Analysis: ...


SYSTEM_PROMPT = """Ты — нутрициолог, который оценивает калорийность еды по фото тарелки.

Правила:
1. Определи все блюда и продукты на фото, оцени вес каждой позиции в граммах и её КБЖУ (ккал, белки, жиры, углеводы в граммах) для этого веса.
2. Оценивай способ приготовления, а не только то, что видно: блеск на еде, жарка, паста, каши, гарниры, заправленные салаты почти всегда содержат добавленный жир (масло, сливочное масло, майонез, заправка). Выдели этот скрытый жир отдельной позицией с "hidden": true и "category": "fat"; если понятно, какой это жир, укажи "fat_type" (например "сливочное", "подсолнечное", "оливковое"). Для видимых продуктов "hidden": false.
3. Если пользователь прислал подпись к фото, она точнее фото: при расхождении доверяй подписи (название блюда, вес, способ приготовления, состав).
4. Каждой позиции присвой категорию строго из списка: main, side, vegetable, fruit, sweet, drink, sauce, fat, other.
5. "ask_about_fat": true, если добавленный жир существенно меняет калорийность и ты не уверен в его количестве (гарниры, жареное, паста и каши, заправленные салаты). Иначе false.
6. "confidence": "high", "medium" или "low" — насколько ты уверен в оценке.
7. Если на фото нет еды, верни "is_food": false и пустой список "items".
8. Все числа неотрицательные; граммы не больше 3000, ккал на позицию не больше 5000. Названия — по-русски.

Отвечай строго одним JSON-объектом, без пояснений, без markdown и без текста до или после. Схема:
{
  "is_food": true,
  "confidence": "high | medium | low",
  "items": [
    {"name": "Вермишель отварная", "grams": 200, "kcal": 230, "protein": 7.0, "fat": 1.0, "carbs": 46.0, "hidden": false, "category": "side"},
    {"name": "Масло в гарнире", "grams": 10, "kcal": 75, "protein": 0.1, "fat": 8.3, "carbs": 0.1, "hidden": true, "category": "fat", "fat_type": "сливочное"}
  ],
  "ask_about_fat": true
}
Поле "fat_type" необязательное, остальные поля обязательны.
Допустимые категории: main, side, vegetable, fruit, sweet, drink, sauce, fat, other.

Если вместе с текстом тебе передан предыдущий результат в JSON и поправка пользователя, примени поправку к этому результату, пересчитай значения и верни полный обновлённый JSON той же схемы."""


# ---------- разбор ответа ----------

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _num(value: Any, limit: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadResponse("not a number")
    if not math.isfinite(value):
        raise BadResponse("not a finite number")
    if value < 0 or (limit is not None and value > limit):
        raise BadResponse("number out of range")
    return float(value)


def parse_analysis(raw: str) -> Analysis:
    text = raw.strip() if isinstance(raw, str) else ""
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise BadResponse("invalid json") from e
    if not isinstance(data, dict):
        raise BadResponse("not an object")
    try:
        is_food = data["is_food"]
        confidence = data["confidence"]
        raw_items = data["items"]
        ask = data["ask_about_fat"]
        if not isinstance(is_food, bool) or not isinstance(ask, bool):
            raise BadResponse("bad flag")
        if confidence not in _CONFIDENCE:
            raise BadResponse("bad confidence")
        if not isinstance(raw_items, list):
            raise BadResponse("items is not a list")
        items = []
        for it in raw_items:
            if not isinstance(it, dict):
                raise BadResponse("item is not an object")
            name = it["name"]
            hidden = it["hidden"]
            if not isinstance(name, str) or not name.strip() or not isinstance(hidden, bool):
                raise BadResponse("bad item")
            category = it["category"]
            if category not in CATEGORIES:
                category = "other"
            fat_type = it.get("fat_type")
            if fat_type is not None and not isinstance(fat_type, str):
                raise BadResponse("bad fat_type")
            items.append(
                FoodItem(
                    name=name.strip(),
                    grams=_num(it["grams"], MAX_GRAMS),
                    kcal=_num(it["kcal"], MAX_KCAL),
                    protein=_num(it["protein"]),
                    fat=_num(it["fat"]),
                    carbs=_num(it["carbs"]),
                    hidden=hidden,
                    category=category,
                    fat_type=fat_type or None,
                )
            )
    except KeyError as e:
        raise BadResponse(f"missing field {e}") from e
    return Analysis(is_food, confidence, tuple(items), ask)


# ---------- подготовка картинки ----------

def prepare_image(data: bytes) -> bytes:
    """JPEG не больше ``MAX_SIDE`` по стороне; ``BadImage``, если файл не картинка или «бомба»."""
    saved_cap = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                im.save(out, "JPEG", quality=85)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise BadImage("too many pixels") from e
    except (OSError, ValueError, SyntaxError) as e:  # PIL: не картинка или битый файл
        raise BadImage(type(e).__name__) from e
    finally:
        Image.MAX_IMAGE_PIXELS = saved_cap
    return out.getvalue()


# ---------- клиенты ----------

def _correction_text(previous: Analysis, text: str) -> str:
    prev = json.dumps(asdict(previous), ensure_ascii=False)
    return f"Предыдущий результат (JSON):\n{prev}\n\nПоправка пользователя: {text}"


def _b64(image_jpeg: bytes) -> str:
    return base64.b64encode(image_jpeg).decode("ascii")


async def _post(http: httpx.AsyncClient, url: str, api_key: str, body: dict) -> Any:
    try:
        resp = await http.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_S,
        )
    except httpx.TimeoutException as e:
        raise VisionError("timeout") from e
    except httpx.HTTPError as e:
        raise VisionError(f"transport error: {type(e).__name__}") from e
    if resp.status_code == 402:
        raise NoCredits("http 402")
    if resp.status_code != 200:
        raise VisionError(f"http {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise BadResponse("response is not json") from e
    code = payload.get("code") if isinstance(payload, dict) else None
    if isinstance(code, int) and not isinstance(code, bool) and code != 200:
        if code == 402:
            raise NoCredits("code 402")
        raise VisionError(f"code {code}")
    return payload


class KieOpenAIClient:
    def __init__(self, model: str, api_key: str, http: httpx.AsyncClient):
        self.model = model
        self._api_key = api_key
        self._http = http

    async def _chat(self, user_content: Any) -> Analysis:
        body = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        }
        url = f"https://api.kie.ai/{self.model}/v1/chat/completions"
        payload = await _post(self._http, url, self._api_key, body)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise BadResponse("unexpected response shape") from e
        if not isinstance(content, str):
            raise BadResponse("content is not text")
        return parse_analysis(content)

    async def analyze(self, image_jpeg: bytes, caption: str | None) -> Analysis:
        return await self._chat(
            [
                {"type": "text", "text": caption or "Подписи нет."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_b64(image_jpeg)}"}},
            ]
        )

    async def correct(self, previous: Analysis, text: str) -> Analysis:
        return await self._chat(_correction_text(previous, text))


class KieClaudeClient:
    def __init__(self, model: str, api_key: str, http: httpx.AsyncClient):
        self.model = model
        self._api_key = api_key
        self._http = http

    async def _messages(self, content: Any) -> Analysis:
        body = {
            "model": self.model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
        }
        payload = await _post(
            self._http, "https://api.kie.ai/claude/v1/messages", self._api_key, body
        )
        try:
            text = next(
                b["text"] for b in payload["content"]
                if isinstance(b, dict) and b.get("type") == "text"
            )
        except (KeyError, TypeError, StopIteration) as e:
            raise BadResponse("unexpected response shape") from e
        return parse_analysis(text)

    async def analyze(self, image_jpeg: bytes, caption: str | None) -> Analysis:
        return await self._messages(
            [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": _b64(image_jpeg)}},
                {"type": "text", "text": caption or "Подписи нет."},
            ]
        )

    async def correct(self, previous: Analysis, text: str) -> Analysis:
        return await self._messages(_correction_text(previous, text))


# ---------- запасная модель ----------

class FallbackVision:
    def __init__(self, primary: VisionClient, fallback: VisionClient | None):
        self.primary = primary
        self.fallback = fallback

    async def _attempt(self, client: VisionClient, op: str, args: tuple) -> Analysis:
        method = getattr(client, op)
        try:
            return await method(*args)
        except BadResponse:
            log.warning("vision bad response, retrying", extra={"error_kind": "bad_response"})
            return await method(*args)

    async def _run(self, op: str, *args: Any) -> Analysis:
        no_credits = False
        for client in (self.primary, self.fallback):
            if client is None:
                continue
            try:
                return await self._attempt(client, op, args)
            except NoCredits:
                no_credits = True
                log.warning("vision no credits", extra={"error_kind": "no_credits"})
            except VisionError as e:
                log.warning("vision failed: %s", e, extra={"error_kind": "vision_error"})
        raise VisionUnavailable("all vision models failed", no_credits=no_credits)

    async def analyze(self, image_jpeg: bytes, caption: str | None) -> Analysis:
        return await self._run("analyze", image_jpeg, caption)

    async def correct(self, previous: Analysis, text: str) -> Analysis:
        return await self._run("correct", previous, text)


def _build_one(spec: str, api_key: str, http: httpx.AsyncClient) -> VisionClient:
    kind, sep, model = spec.partition(":")
    if not sep or not model:
        raise ValueError(f"bad vision spec: {spec!r}")
    if kind == "openai":
        return KieOpenAIClient(model, api_key, http)
    if kind == "claude":
        return KieClaudeClient(model, api_key, http)
    raise ValueError(f"unknown vision kind: {kind!r}")


def build_vision(
    primary: str, fallback: str | None, api_key: str, http: httpx.AsyncClient
) -> FallbackVision:
    return FallbackVision(
        _build_one(primary, api_key, http),
        _build_one(fallback, api_key, http) if fallback else None,
    )
