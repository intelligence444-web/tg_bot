from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from bot.models import Analysis, FoodItem
from bot.vision import (
    BadResponse,
    FallbackVision,
    KieClaudeClient,
    KieOpenAIClient,
    NoCredits,
    VisionError,
    VisionUnavailable,
    build_vision,
    parse_analysis,
    prepare_image,
)
from tests.fakes import FakeVision

GOOD = {
    "is_food": True,
    "confidence": "high",
    "items": [
        {"name": "Вермишель отварная", "grams": 200, "kcal": 230, "protein": 7.0,
         "fat": 1.0, "carbs": 46.0, "hidden": False, "category": "side"},
        {"name": "Масло в гарнире", "grams": 10, "kcal": 75, "protein": 0.1,
         "fat": 8.3, "carbs": 0.1, "hidden": True, "category": "fat",
         "fat_type": "сливочное"},
    ],
    "ask_about_fat": True,
}
GOOD_RAW = json.dumps(GOOD, ensure_ascii=False)


def _with_item(**kw) -> str:
    item = dict(GOOD["items"][0], **kw)
    return json.dumps({**GOOD, "items": [item]})


def _analysis() -> Analysis:
    return parse_analysis(GOOD_RAW)


# ---------- parse_analysis ----------

def test_parse_ok():
    a = parse_analysis(GOOD_RAW)
    assert a.is_food and a.confidence == "high" and a.ask_about_fat
    assert len(a.items) == 2
    assert a.items[0].fat_type is None
    assert a.items[1].fat_type == "сливочное" and a.items[1].hidden
    assert a.kcal == 305


def test_parse_fenced():
    assert parse_analysis(f"```json\n{GOOD_RAW}\n```") == parse_analysis(GOOD_RAW)


def test_parse_not_food():
    a = parse_analysis('{"is_food": false, "confidence": "high", "items": [], "ask_about_fat": false}')
    assert not a.is_food and a.items == ()


@pytest.mark.parametrize("raw", [
    "не json вообще",
    "",
    "[]",
    _with_item(grams=-1),
    _with_item(grams=3001),
    _with_item(kcal=5001),
    _with_item(protein=-0.1),
    '{"is_food": true}',
    json.dumps({**GOOD, "confidence": "sure"}),
])
def test_parse_bad(raw):
    with pytest.raises(BadResponse):
        parse_analysis(raw)


def test_parse_unknown_category_is_other():
    assert parse_analysis(_with_item(category="zzz")).items[0].category == "other"


# ---------- KieOpenAIClient ----------

def _http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _openai_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


async def test_openai_analyze_ok():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_openai_body(GOOD_RAW))

    c = KieOpenAIClient("gemini-3-pro", "KEY", _http(handler))
    a = await c.analyze(b"\xff\xd8jpegbytes", "гречка")
    assert a == _analysis()
    assert seen["url"] == "https://api.kie.ai/gemini-3-pro/v1/chat/completions"
    assert seen["auth"] == "Bearer KEY"
    assert seen["body"]["stream"] is False
    assert "data:image/jpeg;base64," in json.dumps(seen["body"])
    assert seen["body"]["messages"][0]["role"] == "system"


async def test_openai_correct_has_no_image():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["raw"] = req.content.decode()
        return httpx.Response(200, json=_openai_body(GOOD_RAW))

    c = KieOpenAIClient("gemini-3-pro", "KEY", _http(handler))
    await c.correct(_analysis(), "это гречка, 150 г")
    assert "image_url" not in seen["raw"]
    assert "гречка" in json.loads(seen["raw"])["messages"][-1]["content"]
    assert "Вермишель" in json.loads(seen["raw"])["messages"][-1]["content"]


async def test_openai_http_402():
    c = KieOpenAIClient("m", "K", _http(lambda r: httpx.Response(402, json={})))
    with pytest.raises(NoCredits):
        await c.analyze(b"x", None)


async def test_openai_body_code_402():
    c = KieOpenAIClient("m", "K", _http(
        lambda r: httpx.Response(200, json={"code": 402, "msg": "Credits insufficient", "data": None})))
    with pytest.raises(NoCredits):
        await c.analyze(b"x", None)


async def test_openai_body_code_422_is_vision_error():
    c = KieOpenAIClient("m", "K", _http(
        lambda r: httpx.Response(200, json={"code": 422, "msg": "bad model", "data": None})))
    with pytest.raises(VisionError) as e:
        await c.analyze(b"x", None)
    assert not isinstance(e.value, NoCredits)


async def test_openai_http_500():
    c = KieOpenAIClient("m", "K", _http(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(VisionError):
        await c.analyze(b"x", None)


async def test_openai_timeout():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    c = KieOpenAIClient("m", "K", _http(handler))
    with pytest.raises(VisionError):
        await c.analyze(b"x", None)


async def test_openai_garbage_shape_is_bad_response():
    c = KieOpenAIClient("m", "K", _http(lambda r: httpx.Response(200, json={"foo": 1})))
    with pytest.raises(BadResponse):
        await c.analyze(b"x", None)


# ---------- KieClaudeClient ----------

async def test_claude_analyze_ok():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": f"```json\n{GOOD_RAW}\n```"}]})

    c = KieClaudeClient("claude-sonnet-4-5", "KEY", _http(handler))
    a = await c.analyze(b"jpeg", None)
    assert a == _analysis()
    assert seen["url"] == "https://api.kie.ai/claude/v1/messages"
    body = seen["body"]
    assert body["model"] == "claude-sonnet-4-5" and body["max_tokens"] == 2000
    assert body["system"]
    blocks = body["messages"][0]["content"]
    img = next(b for b in blocks if b["type"] == "image")
    assert img["source"]["type"] == "base64" and img["source"]["media_type"] == "image/jpeg"


async def test_claude_correct_no_image_and_402():
    seen = {}

    def handler(req):
        seen["raw"] = req.content.decode()
        return httpx.Response(200, json={"content": [{"type": "text", "text": GOOD_RAW}]})

    c = KieClaudeClient("m", "K", _http(handler))
    await c.correct(_analysis(), "убери масло")
    assert '"image"' not in seen["raw"]

    c2 = KieClaudeClient("m", "K", _http(lambda r: httpx.Response(200, json={"code": 402, "msg": "x"})))
    with pytest.raises(NoCredits):
        await c2.correct(_analysis(), "t")


# ---------- FallbackVision ----------

async def test_fallback_retry_bad_response_same_model():
    p = FakeVision([BadResponse("x"), _analysis()])
    f = FallbackVision(p, FakeVision([]))
    assert await f.analyze(b"x", None) == _analysis()
    assert len(p.calls) == 2


async def test_fallback_primary_error_uses_fallback():
    p = FakeVision([VisionError("x")])
    fb = FakeVision([_analysis()])
    f = FallbackVision(p, fb)
    assert await f.analyze(b"x", "cap") == _analysis()
    assert len(p.calls) == 1 and len(fb.calls) == 1


async def test_fallback_bad_twice_then_fallback():
    p = FakeVision([BadResponse("a"), BadResponse("b")])
    fb = FakeVision([_analysis()])
    assert await FallbackVision(p, fb).analyze(b"x", None) == _analysis()
    assert len(p.calls) == 2


async def test_fallback_both_no_credits():
    f = FallbackVision(FakeVision([NoCredits("a")]), FakeVision([NoCredits("b")]))
    with pytest.raises(VisionUnavailable) as e:
        await f.analyze(b"x", None)
    assert e.value.no_credits is True


async def test_fallback_both_errors_not_no_credits():
    f = FallbackVision(FakeVision([VisionError("a")]), FakeVision([VisionError("b")]))
    with pytest.raises(VisionUnavailable) as e:
        await f.analyze(b"x", None)
    assert e.value.no_credits is False


async def test_fallback_none():
    f = FallbackVision(FakeVision([VisionError("a")]), None)
    with pytest.raises(VisionUnavailable) as e:
        await f.analyze(b"x", None)
    assert e.value.no_credits is False


async def test_fallback_correct_delegates():
    p = FakeVision([VisionError("a")])
    fb = FakeVision([_analysis()])
    assert await FallbackVision(p, fb).correct(_analysis(), "t") == _analysis()
    assert fb.calls[0][0] == "correct"


# ---------- build_vision ----------

def test_build_vision():
    http = httpx.AsyncClient()
    v = build_vision("openai:gemini-3-pro", "claude:claude-sonnet-4-5", "K", http)
    assert isinstance(v, FallbackVision)
    assert isinstance(v.primary, KieOpenAIClient) and v.primary.model == "gemini-3-pro"
    assert isinstance(v.fallback, KieClaudeClient)
    assert build_vision("openai:x", None, "K", http).fallback is None
    with pytest.raises(ValueError):
        build_vision("bogus", None, "K", http)


# ---------- prepare_image ----------

def test_prepare_image_resizes():
    buf = io.BytesIO()
    Image.new("RGBA", (4000, 3000), (200, 10, 10, 255)).save(buf, "PNG")
    out = prepare_image(buf.getvalue())
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and im.mode == "RGB"
    assert max(im.size) == 1280 and im.size == (1280, 960)


def test_prepare_image_small_not_upscaled():
    buf = io.BytesIO()
    Image.new("RGB", (100, 50)).save(buf, "JPEG")
    assert Image.open(io.BytesIO(prepare_image(buf.getvalue()))).size == (100, 50)


def test_prepare_image_exif_rotation():
    buf = io.BytesIO()
    im = Image.new("RGB", (200, 100))
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть на 90 по часовой
    im.save(buf, "JPEG", exif=exif)
    assert Image.open(io.BytesIO(prepare_image(buf.getvalue()))).size == (100, 200)
