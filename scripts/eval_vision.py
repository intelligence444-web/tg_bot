"""Проверка точности моделей распознавания на своих фото (спец. 16).

В папке лежат пары: ``name.jpg`` + ``name.json`` с эталоном
``{"kcal": 520, "protein": 30, "fat": 18, "carbs": 55}`` (блюдо взвешено, КБЖУ посчитано вручную).

    KIE_API_KEY=... python scripts/eval_vision.py photos/ --models openai:gemini-3-pro openai:gemini-2.5-pro claude:<id>

Для каждой модели печатается: средняя абсолютная ошибка ккал, средняя ошибка в %, число сбоев,
среднее время ответа. Каждая модель проверяется отдельно, без запасной. Содержимое фото не печатается.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


@dataclass(frozen=True)
class Sample:
    name: str
    image: Path
    kcal: float
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None


@dataclass
class ModelResult:
    model: str
    abs_errors: list[float] = field(default_factory=list)
    pct_errors: list[float] = field(default_factory=list)
    times: list[float] = field(default_factory=list)
    failures: int = 0

    @property
    def mae(self) -> float | None:
        return sum(self.abs_errors) / len(self.abs_errors) if self.abs_errors else None

    @property
    def mape(self) -> float | None:
        return sum(self.pct_errors) / len(self.pct_errors) if self.pct_errors else None

    @property
    def avg_time(self) -> float | None:
        return sum(self.times) / len(self.times) if self.times else None


def load_samples(folder: Path) -> list[Sample]:
    samples = []
    for image in sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES):
        ref_path = image.with_suffix(".json")
        if not ref_path.exists():
            print(f"пропуск {image.name}: нет {ref_path.name}", file=sys.stderr)
            continue
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        samples.append(
            Sample(
                name=image.stem,
                image=image,
                kcal=float(ref["kcal"]),
                protein=_opt(ref.get("protein")),
                fat=_opt(ref.get("fat")),
                carbs=_opt(ref.get("carbs")),
            )
        )
    return samples


def _opt(value: Any) -> float | None:
    return None if value is None else float(value)


Analyze = Callable[[bytes], Awaitable[Any]]


async def evaluate_model(
    model: str, analyze: Analyze, samples: Sequence[Sample], images: dict[str, bytes], verbose: bool = False
) -> ModelResult:
    result = ModelResult(model)
    for s in samples:
        started = time.perf_counter()
        try:
            analysis = await analyze(images[s.name])
        except Exception as e:  # noqa: BLE001 — любой сбой модели считаем сбоем
            result.failures += 1
            if verbose:
                print(f"  {model} {s.name}: сбой ({type(e).__name__})", file=sys.stderr)
            continue
        result.times.append(time.perf_counter() - started)
        if not analysis.is_food:
            result.failures += 1
            if verbose:
                print(f"  {model} {s.name}: модель не увидела еды", file=sys.stderr)
            continue
        err = abs(analysis.kcal - s.kcal)
        result.abs_errors.append(err)
        if s.kcal > 0:
            result.pct_errors.append(err / s.kcal * 100)
        if verbose:
            print(f"  {model} {s.name}: {analysis.kcal:.0f} ккал (эталон {s.kcal:.0f})", file=sys.stderr)
    return result


def _fmt(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def format_table(results: Sequence[ModelResult], total: int) -> str:
    header = ("модель", "ошибка ккал", "ошибка %", "сбои", "время, с")
    rows = [
        (r.model, _fmt(r.mae), _fmt(r.mape, 1), f"{r.failures}/{total}", _fmt(r.avg_time, 1))
        for r in results
    ]
    widths = [max(len(str(row[i])) for row in (header, *rows)) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)) for row in (header, *rows)]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


async def run(folder: Path, models: Sequence[str], api_key: str, verbose: bool = False) -> list[ModelResult]:
    import httpx  # noqa: PLC0415

    from bot.vision import build_vision, prepare_image  # noqa: PLC0415

    samples = load_samples(folder)
    if not samples:
        raise SystemExit(f"в папке {folder} нет пар фото + json")
    images = {s.name: prepare_image(s.image.read_bytes()) for s in samples}
    results = []
    async with httpx.AsyncClient() as http:
        for model in models:
            vision = build_vision(model, None, api_key, http)
            results.append(
                await evaluate_model(model, lambda img, v=vision: v.analyze(img, None), samples, images, verbose)
            )
    print(f"Фото: {len(samples)}")
    print(format_table(results, len(samples)))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сравнить модели Kie по точности ккал на папке с фото (name.jpg + name.json). "
        "Нужна переменная KIE_API_KEY."
    )
    parser.add_argument("folder", type=Path, help="папка с парами name.jpg + name.json")
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        metavar="MODEL",
        help='модели в формате VISION_PRIMARY, например "openai:gemini-3-pro" "claude:<id>"',
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="печатать результат по каждому фото")
    args = parser.parse_args(argv)
    for spec in args.models:
        kind, sep, model = spec.partition(":")
        if kind not in ("openai", "claude") or not sep or not model:
            parser.error(f"неверный формат модели: {spec!r} (нужно openai:<модель> или claude:<модель>)")
    if not args.folder.is_dir():
        parser.error(f"нет папки {args.folder}")
    api_key = os.environ.get("KIE_API_KEY")
    if not api_key:
        parser.error("не задана переменная окружения KIE_API_KEY")
    asyncio.run(run(args.folder, args.models, api_key, args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
