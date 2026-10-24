# Calorie Bot MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram-бот: анкета с нормой КБЖУ, калории по фото, дневник, взвешивания с прогрессом и поздравлениями, подсказки и курс «Знание дня».

**Architecture:** Чистая логика (`calc`, `progress`, `tips`, `diary`, `models`) без Telegram и базы — покрыта модульными тестами. Хранилище за протоколом `Storage` с двумя реализациями: `MemoryStorage` (тесты) и `YdbStorage` (прод). Обработчики aiogram получают зависимости (`storage`, `vision`, `clock`, `fetch_photo`) через данные диспетчера, поэтому тестируются подачей `Update` в диспетчер с подменённой сессией бота. Две точки входа Cloud Functions: `bot.main.handler` (вебхук) и `bot.daily.handler` (таймер 09:00 МСК).

**Tech Stack:** Python 3.12, aiogram 3, httpx, Pillow, matplotlib, ydb (Python SDK), pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-24-calorie-bot-mvp-design.md` — разделы ниже ссылаются на него как «спец. N».

## Global Constraints

- Python 3.12; рантайм Cloud Functions `python312`.
- Зависимости только: `aiogram>=3.13,<4`, `httpx>=0.27`, `Pillow>=10`, `matplotlib>=3.8`, `ydb>=3.18`; dev: `pytest>=8`, `pytest-asyncio>=0.23`.
- Часовой пояс всех «дат дня»: `Europe/Moscow` через `zoneinfo`. В базе время в UTC, `local_date` — дата по Москве.
- Все тексты пользователю — только в `bot/texts.py` (и карточки — в `bot/course.py`). Тон — спец. 5: нет слов «нельзя», «запрещено», «сорвался», «плохо»; не больше 2 эмодзи на сообщение.
- Фото не сохраняются нигде: ни в базе, ни на диске, ни в логах.
- Числа в сообщениях: `round_half_up`; ккал и граммы до целого, вода до 0,1 л, кг и темп до 0,1, недели до целого. Разделитель тысяч — пробел («1 761»), десятичный — запятая («0,4»).
- Секреты только из переменных окружения (спец. 17); в репозитории `.env.example` без значений.
- Логи: `logging` с полями `user_id` и `error_kind`, без содержимого фото и ответов модели целиком.
- Тесты: `pytest -q` из корня проходит без сети и без YDB; тесты YDB помечены `@pytest.mark.ydb` и пропускаются без `YDB_ENDPOINT`.

## Review Focus

1. Число с запятой, пробелами и единицами («70,5 кг», « 165 см») в анкете и взвешивании — принимается как 70.5 / 165.
2. Фото, отправленное файлом (документ `image/*`), — обрабатывается как обычное фото.
3. Альбом из нескольких фото (`media_group_id`) — анализируется только первое, на остальные одна подсказка «Отправляй по одному фото на приём пищи».
4. Нажатие кнопки на старом сообщении, когда запись уже удалена, — ответ «Эта запись уже удалена», без падения.
5. Приём пищи в 23:30 по Москве (20:30 UTC) и в 00:30 по Москве (21:30 UTC предыдущего дня) — попадает в правильный день по Москве.

Тест на каждый пункт добавлен в задачу, которой принадлежит код (помечен «Review Focus N»).

---

## File Structure

```
bot/
  __init__.py
  models.py       # FoodItem, Analysis, Meal, User, Weighing — общие dataclass
  calc.py         # нормы (спец. 7), parse_number, round_half_up, format_num
  progress.py     # тренд, процент, темп, прогноз, этапы, серия, «вес вырос» (спец. 8, 9)
  tips.py         # подсказка после фото (спец. 10)
  diary.py        # тип приёма пищи, итоги дня, текст дневника (спец. 6.5)
  chart.py        # PNG графика (спец. 6.8)
  vision.py       # клиенты Kie, разбор ответа, запасная модель, подготовка фото (спец. 12)
  storage.py      # протокол Storage, MemoryStorage
  ydb_storage.py  # YdbStorage, YdbFsmStorage, create_tables
  texts.py        # все тексты
  course.py       # карточки «Знание дня» (спец. 11)
  keyboards.py    # клавиатуры и формат callback_data
  handlers/
    __init__.py   # build_router()
    start.py      # /start, согласие, /stop_tips, /start_tips, помощь
    onboarding.py # анкета (спец. 6.2–6.3)
    meals.py      # фото, масло, изменить/удалить/сменить тип (спец. 6.4)
    diary.py      # «📅 Сегодня»
    weight.py     # взвешивание, прогресс, график, цель достигнута (спец. 6.6–6.8, 9)
    profile.py    # «👤 Мой профиль»
    fallback.py   # текст вне сценария
  app.py          # build_dispatcher(deps) — общая сборка для main и тестов
  main.py         # Cloud Function: вебхук
  daily.py        # Cloud Function: рассылки
tests/
  conftest.py     # фикстуры: storage (memory/ydb), fake bot session, clock, fake vision
  fakes.py        # MockedSession, FakeVision, FixedClock
  test_*.py
scripts/
  build_zip.sh    # сборка архива для функций
  eval_vision.py  # ручная проверка точности моделей
docs/deploy.md
requirements.txt
requirements-dev.txt
pytest.ini
.env.example
README.md
```

---

### Task 1: Каркас проекта, модели и расчёт нормы

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `bot/__init__.py`, `bot/models.py`, `bot/calc.py`
- Test: `tests/test_calc.py`

**Interfaces:**
- Produces (`bot/models.py`):
  - `Sex = Literal["male", "female"]`, `Activity = Literal["sedentary", "light", "moderate", "high", "very_high"]`, `Goal = Literal["lose", "maintain", "gain"]`, `MealType = Literal["breakfast", "lunch", "snack", "dinner"]`, `Category = Literal["main","side","vegetable","fruit","sweet","drink","sauce","fat","other"]`
  - `@dataclass(frozen=True) Profile(sex, age: int, height_cm: float, weight_kg: float, activity, goal, target_weight_kg: float | None)`
  - `@dataclass(frozen=True) Norm(bmr, tdee, kcal, protein_g, fat_g, carbs_g, water_ml: float)`
  - `@dataclass(frozen=True) FoodItem(name: str, grams: float, kcal: float, protein: float, fat: float, carbs: float, hidden: bool, category: Category, fat_type: str | None = None)`
  - `@dataclass(frozen=True) Analysis(is_food: bool, confidence: Literal["high","medium","low"], items: tuple[FoodItem, ...], ask_about_fat: bool)` с свойствами `kcal`, `protein`, `fat`, `carbs` (суммы по `items`) и методом `with_fat(grams: float) -> Analysis` (заменяет все `hidden` позиции категории `fat` одной позицией «Масло» указанного веса; тип жира берётся из прежней скрытой позиции, если он был; `grams == 0` — скрытых жиров нет; КБЖУ на 100 г: сливочное 748 / 0,5 / 82,5 / 0,8, иначе подсолнечное 899 / 0 / 99,9 / 0)
  - `@dataclass Meal(user_id: int, meal_id: str, eaten_at: datetime, local_date: date, meal_type: MealType, analysis: Analysis, fat_answer: str | None = None)`
  - `@dataclass(frozen=True) Weighing(day: date, weight_kg: float)`
  - `@dataclass User(user_id: int, consent_at: datetime | None = None, profile: Profile | None = None, norm: Norm | None = None, start_weight_kg: float | None = None, goal_started_at: date | None = None, onboarded_at: date | None = None, maintain_transition_until: date | None = None, milestones_sent: set[str] = field(default_factory=set), course_day: int = 0, tips_enabled: bool = True, is_active: bool = True, last_overeat_date: date | None = None, last_tip_variants: dict[str, int] = field(default_factory=dict))`
- Produces (`bot/calc.py`):
  - `ACTIVITY_FACTORS: dict[Activity, float]` = sedentary 1.2, light 1.375, moderate 1.55, high 1.725, very_high 1.9
  - `bmr(sex, weight_kg, height_cm, age) -> float`
  - `calculate_norm(p: Profile, kcal_override: float | None = None) -> Norm` — спец. 7; `kcal_override` заменяет ккал (для плавного выхода), БЖУ считаются от неё
  - `planned_pace_kg_week(norm: Norm) -> float` — `|kcal − tdee| × 7 / 7700`
  - `planned_weeks_to_goal(p: Profile, norm: Norm) -> float | None` — `None` для maintain или нулевой разницы
  - `bmi(weight_kg, height_cm) -> float`
  - `lose_allowed(age: int) -> bool` — `False` для 14–15
  - `target_bmi_low(target_kg, height_cm) -> bool` — ИМТ < 18.5
  - `round_half_up(x: float, ndigits: int = 0) -> float` (через `Decimal`, `ROUND_HALF_UP`)
  - `format_num(x: float, ndigits: int = 0) -> str` — «1 761», «0,4», «2,1»; нулевая дробная часть отбрасывается (1.0 при ndigits=1 → «1»)
  - `parse_number(text: str, lo: float, hi: float) -> float | None` — убирает пробелы и буквенные единицы, запятая = точка; вне диапазона или не число → `None`

- [ ] **Step 1: Создать каркас.** `requirements.txt` и `requirements-dev.txt` по Global Constraints (`requirements-dev.txt` начинается с `-r requirements.txt`). `pytest.ini`: `asyncio_mode = auto`, `markers = ydb: needs YDB_ENDPOINT`, `testpaths = tests`. `.gitignore`: `__pycache__/`, `.env`, `.venv/`, `dist/`, `*.zip`.

- [ ] **Step 2: Написать падающие тесты `tests/test_calc.py`**

```python
P = Profile("female", 30, 165, 70, "moderate", "lose", 62)

def test_reference_example():
    n = calculate_norm(P)
    assert n.bmr == pytest.approx(1420.25)
    assert n.tdee == pytest.approx(2201.3875)
    assert n.kcal == pytest.approx(1761.11)
    assert round_half_up(n.protein_g) == 126 and round_half_up(n.fat_g) == 63
    assert round_half_up(n.carbs_g) == 173
    assert n.water_ml == pytest.approx(2100)
    assert round_half_up(planned_pace_kg_week(n), 1) == 0.4
    assert round_half_up(planned_weeks_to_goal(P, n)) == 20

def test_male_bmr():
    assert bmr("male", 80, 180, 40) == pytest.approx(1730)

@pytest.mark.parametrize("activity,f", [("sedentary",1.2),("light",1.375),("moderate",1.55),("high",1.725),("very_high",1.9)])
def test_activity_factors(activity, f): ...  # tdee == bmr * f

def test_lose_never_below_bmr():  # sedentary, крупный дефицит: kcal == bmr
def test_gain_plus_10_percent():  # kcal == tdee * 1.1, protein 1.6 г/кг
def test_maintain():               # kcal == tdee, protein 1.4 г/кг, weeks is None
def test_carbs_not_negative():     # очень лёгкий человек с override=500 → carbs_g == 0
def test_kcal_override():          # calculate_norm(P, 2000).kcal == 2000, углеводы от 2000
def test_lose_allowed():           # 14,15 → False; 16 → True
def test_target_bmi_low():         # 165 см: 50 кг → True (18.37), 51 кг → False
def test_round_half_up():          # 172.5 → 173; 0.45 (1 знак) → 0.5
def test_format_num():             # 1761.11 → "1 761"; 0.4 → "0,4"; 2.1 → "2,1"; 1.0 (1 знак) → "1"

@pytest.mark.parametrize("text,expected", [("70",70),("70,5 кг",70.5),(" 165 см",165),("70.5",70.5),("абв",None),("500",None),("",None)])
def test_parse_number(text, expected):  # Review Focus 1; диапазон 30–300
    assert parse_number(text, 30, 300) == expected
```

Плюс `tests/test_models.py`: `Analysis.kcal` — сумма; `with_fat(10)` для скрытого «сливочное» даёт позицию 10 г / 74.8 ккал; `with_fat(0)` убирает скрытые жиры; без указанного типа — подсолнечное, 20 г → 179.8 ккал.

- [ ] **Step 3: Запустить** `pytest tests/test_calc.py tests/test_models.py -q` — Expected: FAIL (ImportError).
- [ ] **Step 4: Реализовать** `bot/models.py` и `bot/calc.py` по Interfaces.
- [ ] **Step 5: Запустить** те же тесты — Expected: PASS.
- [ ] **Step 6: Коммит** `git commit -m "Каркас, модели и расчёт нормы КБЖУ"`

---

### Task 2: Прогресс веса

**Files:**
- Create: `bot/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: `Weighing`, `Goal` из Task 1.
- Produces:
  - `trend_weight(weighings: list[Weighing], on: date) -> float | None` — спец. 8.1
  - `trend_series(weighings) -> list[tuple[date, float]]` — тренд на дату каждого взвешивания (для графика)
  - `passed_kg(start, trend, target) -> float` и `progress_percent(start, trend, target) -> float` — спец. 8.2
  - `actual_pace(weighings, today: date) -> float | None` — кг/нед со знаком (минус — вес падает), спец. 8.3
  - `forecast_date(trend, target, pace: float, today: date) -> date | None` — `None`, если темп не к цели или 0
  - `MILESTONE_ORDER = ("first_kg", "five_percent", "q25", "q50", "q75", "goal")` — старший — последний в этом порядке
  - `new_milestones(start, trend, target, goal: Goal, sent: set[str]) -> list[str]` — достигнутые и не отправленные, в порядке `MILESTONE_ORDER`; `five_percent` только для `lose`
  - `streak(meal_days: set[date], today: date) -> int` — спец. 8.4
  - `@dataclass(frozen=True) Drift(weekly_up: bool, three_weeks: bool)` и `drift(weighings, today, target, goal) -> Drift` — спец. 9.4 (для `maintain` направление «от цели» = любое удаление от `target`)

- [ ] **Step 1: Написать падающие тесты**

```python
def W(d, kg): return Weighing(date(2026, 11, d), kg)

def test_trend_window_average():   # W(1,70),W(5,69),W(8,68): on 8.11 → (69+68)/2 = 68.5
def test_trend_falls_back_to_last():  # W(1,70); on 20.11 → 70
def test_trend_none_without_data():   # [] → None
def test_percent():                  # start 70, trend 66.8, target 62 → passed 3.2, percent 40
def test_percent_moving_away_is_zero():  # trend 71 → 0
def test_percent_capped():           # trend 61 → 100
def test_percent_gain():             # start 60, trend 62, target 68 → 25
def test_actual_pace_needs_7_days(): # 2 взвешивания с разницей 3 дня → None
def test_actual_pace_regression():   # 70 в день 1, 69.3 в день 15 → ≈ −0.35 кг/нед
def test_forecast():                 # trend 66, target 62, pace −0.5, today 1.11.2026 → +8 недель = 27.12.2026
def test_forecast_wrong_direction(): # pace +0.2 при похудении → None
def test_milestones_several_at_once():  # start 70, trend 66.5, target 62, sent={} → ["first_kg","five_percent","q25"]; trend 67.5 → ["first_kg","q25"] (5% = 3,5 кг ещё не пройдены)
def test_milestones_not_repeated():  # sent={"first_kg"} → без first_kg
def test_five_percent_only_lose():   # goal gain → нет five_percent
def test_goal_milestone():           # trend 61.9 → "goal" в списке
def test_streak_today_logged():      # дни 3,4,5; today 5 → 3
def test_streak_today_not_logged():  # дни 3,4; today 5 → 2
def test_streak_broken():            # дни 1,3,4; today 4 → 2
def test_drift_weekly_up():          # тренд неделю назад 68.0, сейчас 68.4, цель 62 lose → weekly_up True
def test_drift_small_noise():        # +0.3 ровно → False
def test_drift_three_weeks():        # тренд сейчас дальше от цели, чем 7, 14 и 21 день назад → three_weeks True
```

- [ ] **Step 2: Запустить** `pytest tests/test_progress.py -q` — FAIL.
- [ ] **Step 3: Реализовать `bot/progress.py`.** Регрессия — обычный МНК по (дни от первой точки, кг) за 28 дней до `today` включительно; наклон × 7.
- [ ] **Step 4: Запустить** — PASS.
- [ ] **Step 5: Коммит** `"Расчёты прогресса: тренд, темп, прогноз, этапы, серия"`

---

### Task 3: Подсказки после фото и дневник

**Files:**
- Create: `bot/tips.py`, `bot/diary.py`, `bot/texts.py` (начать файл: тексты подсказок и дневника; дальше дополняют другие задачи)
- Test: `tests/test_tips.py`, `tests/test_diary.py`

**Interfaces:**
- Consumes: `Analysis`, `Meal`, `MealType`, `Norm`, `format_num`, `streak`.
- Produces:
  - `@dataclass(frozen=True) Tip(rule: str, variant: int, text: str)`
  - `choose_tip(analysis: Analysis, meal_type: MealType, day_kcal_before: float, norm_kcal: float, overeat_sent_today: bool, last_variants: dict[str, int], rng: random.Random) -> Tip | None` — спец. 10 + 8.5. Правило `overeat` срабатывает, если `day_kcal_before <= norm_kcal < day_kcal_before + analysis.kcal` и `not overeat_sent_today`. Если `day_kcal_before > norm_kcal` (перебор уже был), правило 1 пропускается и проверяются 2–6. Для `overeat` один текст (спец. 8.5), `variant = 0`. Для остальных — 3 варианта, `variant` ≠ `last_variants.get(rule)`.
  - `texts.TIPS: dict[str, tuple[str, ...]]` — ключи `low_protein`, `drink`, `sweet`, `no_veg`, `high_protein`; первый вариант каждого — точный текст из спец. 10; `drink` содержит `{kcal}`.
  - `MSK = ZoneInfo("Europe/Moscow")`, `to_local(dt_utc: datetime) -> datetime`
  - `meal_type_for(local: datetime) -> MealType` — спец. 6.5
  - `@dataclass DaySummary(kcal, protein, fat, carbs: float, by_type: dict[MealType, float])` и `summarize(meals: list[Meal]) -> DaySummary`
  - `format_day(summary: DaySummary, norm: Norm, streak_days: int) -> str` — ровно макет спец. 6.5; полоса из 10 символов `▓`/`░`, заполнено `min(10, floor(kcal / norm × 10))`, процент — `round_half_up`

- [ ] **Step 1: Падающие тесты `tests/test_tips.py`:** по тесту на каждое правило (входные данные — минимальный `Analysis`, подходящий только под это правило); порядок приоритета (мало белка + сладкое → `low_protein`); `overeat` один раз (флаг `overeat_sent_today=True` → следующее правило); перебор уже был до приёма → `overeat` не срабатывает; `no_veg` не для завтрака и перекуса; `drink` порог 150 включительно; нет подходящих → `None`; вариант не повторяет прошлый (100 вызовов с `last_variants={"sweet": 0}` → ни разу `variant == 0`).

- [ ] **Step 2: Падающие тесты `tests/test_diary.py`:**

```python
@pytest.mark.parametrize("hm,expected", [("04:59","snack"),("05:00","breakfast"),("10:59","breakfast"),("11:00","lunch"),
    ("15:59","lunch"),("16:00","snack"),("16:59","snack"),("17:00","dinner"),("22:59","dinner"),("23:00","snack")])
def test_meal_type_boundaries(hm, expected): ...

def test_to_local_midnight():  # Review Focus 5: 2026-11-01 20:30 UTC → 23:30 1.11; 21:30 UTC → 00:30 2.11

def test_format_day_reference():
    # завтрак 380, обед 545, перекус 320 (два по 160), норма 1761.11 из Task 1, streak 5
    text = format_day(summary, norm, 5)
    assert "📅 Сегодня — 1 245 / 1 761 ккал  ▓▓▓▓▓▓▓░░░ 71%" in text
    assert "🔥 5 дней подряд" in text
    assert "🍎 Перекус — 320 ккал" in text
    assert "🌙 Ужин — пока пусто · можно ещё 516 ккал" in text
    assert text.index("💪 Белок") < text.index("Жиры")

def test_format_day_no_streak_line_below_2(): ...
def test_format_day_over_norm():  # остаток 0, полоса 10/10, процент > 100
def test_streak_word_forms():     # 2 дня, 5 дней, 21 день
```

- [ ] **Step 3: Запустить** `pytest tests/test_tips.py tests/test_diary.py -q` — FAIL.
- [ ] **Step 4: Реализовать** `bot/tips.py`, `bot/diary.py`, нужные тексты в `bot/texts.py`; склонение «день/дня/дней» — функция `plural(n, one, few, many)` в `texts.py`.
- [ ] **Step 5: Запустить** — PASS.
- [ ] **Step 6: Коммит** `"Подсказки после фото и форматирование дневника"`

---

### Task 4: График веса

**Files:**
- Create: `bot/chart.py`
- Test: `tests/test_chart.py`

**Interfaces:**
- Consumes: `Weighing`, `trend_series`.
- Produces: `render_weight_chart(weighings: list[Weighing], target_kg: float, forecast: tuple[date, float] | None) -> bytes` — PNG; точки взвешиваний, линия `trend_series`, горизонтальный пунктир цели, пунктир от последней точки тренда до `forecast`. `matplotlib.use("Agg")` до импорта `pyplot`; фигура закрывается после рендера.

- [ ] **Step 1: Падающие тесты:** для 2 точек без прогноза и для 50 точек с прогнозом результат начинается с `b"\x89PNG"` и длина > 1000; 20 вызовов подряд не увеличивают `len(plt.get_fignums())`.
- [ ] **Step 2: Запустить** — FAIL. **Step 3: Реализовать.** **Step 4: Запустить** — PASS.
- [ ] **Step 5: Коммит** `"График веса"`

---

### Task 5: Распознавание фото

**Files:**
- Create: `bot/vision.py`, `tests/fakes.py` (класс `FakeVision`)
- Test: `tests/test_vision.py`

**Interfaces:**
- Consumes: `Analysis`, `FoodItem`.
- Produces:
  - Исключения: `VisionError(Exception)`; `BadResponse(VisionError)`; `NoCredits(VisionError)`; `VisionUnavailable(VisionError)` с атрибутом `no_credits: bool`.
  - `class VisionClient(Protocol)`: `async analyze(image_jpeg: bytes, caption: str | None) -> Analysis`; `async correct(previous: Analysis, text: str) -> Analysis` (без фото: прошлый JSON + текст пользователя).
  - `parse_analysis(raw: str) -> Analysis` — снимает обёртку ```` ```json ````; проверки спец. 12; нарушение → `BadResponse`; неизвестная категория → `"other"`; отсутствующий `fat_type` → `None`.
  - `prepare_image(data: bytes) -> bytes` — Pillow: учёт EXIF-поворота, RGB, наибольшая сторона ≤ 1280, JPEG quality 85.
  - `SYSTEM_PROMPT: str` — требования спец. 12 и JSON-схема с перечнем категорий, по-русски.
  - `KieOpenAIClient(model: str, api_key: str, http: httpx.AsyncClient)` — `POST https://api.kie.ai/{model}/v1/chat/completions`, `Authorization: Bearer {api_key}`, сообщения: system = `SYSTEM_PROMPT`, user = `[{"type":"text","text": caption-или-пусто}, {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]`, `stream: false`; ответ — `choices[0].message.content`.
  - `KieClaudeClient(model: str, api_key: str, http)` — `POST https://api.kie.ai/claude/v1/messages`, тело в формате Anthropic Messages (`model`, `max_tokens: 2000`, `system`, контент `image` с `source.type = "base64"`); ответ — первый блок `text`.
  - Оба: таймаут 45 с; HTTP 402 или JSON-ответ с `code == 402` → `NoCredits`; иной не-200, `code != 200` в теле или таймаут → `VisionError`.
  - `FallbackVision(primary: VisionClient, fallback: VisionClient | None)` — реализует `VisionClient`: `BadResponse` → один повтор той же модели; затем любая `VisionError` → запасная (по тем же правилам); обе не справились → `VisionUnavailable(no_credits=<был ли NoCredits>)`.
  - `build_vision(primary: str, fallback: str | None, api_key: str, http) -> FallbackVision` — формат строки `"openai:gemini-3-pro"` или `"claude:<model-id>"`.
  - `tests/fakes.py: FakeVision(results: list[Analysis | Exception])` — отдаёт по очереди, запоминает вызовы.

- [ ] **Step 1: Падающие тесты** (HTTP подменяется `httpx.MockTransport`):
  - `parse_analysis`: корректный пример из спец. 12; в обёртке ```` ```json ````; `is_food: false` с пустым `items`; мусор → `BadResponse`; отрицательные граммы → `BadResponse`; граммы 3001 → `BadResponse`; ккал 5001 → `BadResponse`; неизвестная категория → `"other"`.
  - `KieOpenAIClient`: проверяет URL `https://api.kie.ai/gemini-3-pro/v1/chat/completions`, заголовок авторизации, наличие `data:image/jpeg;base64,` в теле; 402 → `NoCredits`; тело `{"code":402,...}` с HTTP 200 → `NoCredits`; `httpx.TimeoutException` → `VisionError`.
  - `KieClaudeClient`: URL `https://api.kie.ai/claude/v1/messages`, блок `image` в теле, текст ответа разбирается.
  - `FallbackVision`: основной `BadResponse` затем успех → успех и 2 вызова основного; основной `VisionError` → запасной; оба `NoCredits` → `VisionUnavailable(no_credits=True)`; основной `VisionError`, запасной `VisionError` → `no_credits=False`; `fallback=None` → сразу `VisionUnavailable`.
  - `prepare_image`: PNG 4000×3000 → JPEG с наибольшей стороной 1280.
- [ ] **Step 2: Запустить** `pytest tests/test_vision.py -q` — FAIL.
- [ ] **Step 3: Реализовать** `bot/vision.py`, `FakeVision`.
- [ ] **Step 4: Запустить** — PASS.
- [ ] **Step 5: Коммит** `"Распознавание фото через Kie с запасной моделью"`

---

### Task 6: Хранилище: протокол и MemoryStorage

**Files:**
- Create: `bot/storage.py`, `tests/conftest.py` (фикстура `storage`, параметризованная `memory` и `ydb`)
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `User`, `Meal`, `Weighing`.
- Produces `class Storage(Protocol)` (все методы `async`):
  - `get_user(user_id: int) -> User | None`; `save_user(user: User) -> None`
  - `add_meal(meal: Meal)`; `update_meal(meal: Meal)`; `get_meal(user_id, meal_id) -> Meal | None`; `delete_meal(user_id, meal_id) -> bool`
  - `meals_on(user_id, day: date) -> list[Meal]` (по `eaten_at`); `meal_days(user_id, since: date) -> set[date]`
  - `upsert_weight(user_id, day: date, weight_kg: float)`; `weights(user_id) -> list[Weighing]` (по дате)
  - `mark_processed(key: str, now: datetime) -> bool` — `True`, если ключ новый (используется для `update:{id}` и `album:{media_group_id}`); записи старше 1 суток можно удалять
  - `alert_allowed(kind: str, now: datetime, interval: timedelta) -> bool` — `True` и запоминает `now`, если прошлое срабатывание старше `interval`
  - `daily_users() -> list[User]` — `is_active and tips_enabled and onboarded_at is not None`
  - `class MemoryStorage(Storage)` — словари в памяти; возвращает копии (`copy.deepcopy`), чтобы тесты ловили забытый `save_user`.

- [ ] **Step 1: Падающие контрактные тесты** (выполняются для каждой реализации через фикстуру): сохранение и чтение `User` со всеми полями, включая `set` и `dict`; изменение возвращённого объекта без `save_user` не меняет хранилище; приёмы пищи за день по `local_date`, порядок по времени; `delete_meal` несуществующего → `False`; `upsert_weight` в тот же день заменяет значение; `mark_processed` — второй раз `False`; `alert_allowed` — второй вызов через 30 мин `False`, через 61 мин `True`; `daily_users` отбрасывает неактивных, отключивших и без анкеты.
- [ ] **Step 2: Запустить** `pytest tests/test_storage.py -q` — FAIL (ydb-вариант пропускается).
- [ ] **Step 3: Реализовать** `bot/storage.py`.
- [ ] **Step 4: Запустить** — PASS для `memory`.
- [ ] **Step 5: Коммит** `"Протокол хранилища и MemoryStorage"`

---

### Task 7: YdbStorage и хранилище состояний aiogram

**Files:**
- Create: `bot/ydb_storage.py`
- Modify: `tests/conftest.py` (ydb-вариант фикстуры: создаёт базу с уникальным префиксом таблиц)
- Test: `tests/test_storage.py` (те же тесты), `tests/test_ydb_fsm.py`

**Interfaces:**
- Consumes: `Storage`.
- Produces:
  - `async create_tables(pool: ydb.aio.QuerySessionPool, prefix: str = "") -> None` — таблицы спец. 14 (`users`, `meals`, `weights`, `fsm`, `processed_updates` с TTL 1 сутки по `created_at`, `alerts`); `processed_updates.key` — `Utf8`.
  - `YdbStorage(pool, prefix: str = "")` — реализует `Storage`; сложные поля (`profile`, `norm`, `milestones_sent`, `last_tip_variants`, `analysis`) хранятся колонкой `Json`.
  - `YdbFsmStorage(pool, prefix="")` — наследник `aiogram.fsm.storage.base.BaseStorage` (`set_state`, `get_state`, `set_data`, `get_data`, `close`), ключ — `f"{bot_id}:{chat_id}:{user_id}"`.
  - `async connect(endpoint: str, database: str) -> ydb.aio.QuerySessionPool` — авторизация через метаданные Cloud Functions (`ydb.iam.MetadataUrlCredentials`), локально — `ydb.credentials_from_env_variables()`.

- [ ] **Step 1: Падающий тест `tests/test_ydb_fsm.py`** (`@pytest.mark.ydb`): `set_state` / `get_state` / `set_data` / `get_data` и `set_state(None)` очищает состояние.
- [ ] **Step 2: Поднять локальную YDB** `docker run -d --rm --name ydb -p 2136:2136 -e GRPC_PORT=2136 -e YDB_USE_IN_MEMORY_PDISKS=true ydbplatform/local-ydb:latest` и запустить `YDB_ENDPOINT=grpc://localhost:2136 YDB_DATABASE=/local YDB_ANONYMOUS_CREDENTIALS=1 pytest -m ydb -q` — FAIL. Если Docker недоступен — записать это в отчёт задачи, тесты ydb остаются пропущенными, проверка переносится на первый деплой (Task 12, шаг проверки).
- [ ] **Step 3: Реализовать** `bot/ydb_storage.py`; `upsert` по первичным ключам, параметризованные запросы YQL (без подстановки строк).
- [ ] **Step 4: Запустить** ydb-тесты — PASS; `pytest -q` без YDB — PASS (ydb пропущены).
- [ ] **Step 5: Коммит** `"Хранилище YDB и состояния анкеты"`

---

### Task 8: Сборка бота, /start, согласие и анкета

**Files:**
- Create: `bot/app.py`, `bot/keyboards.py`, `bot/handlers/__init__.py`, `bot/handlers/start.py`, `bot/handlers/onboarding.py`, `bot/handlers/fallback.py`
- Modify: `bot/texts.py`, `tests/fakes.py`, `tests/conftest.py`
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: `calc`, `Storage`, `Profile`, `User`.
- Produces:
  - `@dataclass Deps(storage: Storage, vision: VisionClient, fetch_photo: Callable[[Bot, str], Awaitable[bytes]], clock: Callable[[], datetime], admin_chat_id: int | None, rng: random.Random)` в `bot/app.py`
  - `build_dispatcher(deps: Deps, fsm_storage: BaseStorage) -> Dispatcher` — регистрирует `build_router()`, кладёт поля `Deps` в данные диспетчера (`dp["storage"] = ...`), обработчики получают их аргументами.
  - `keyboards.main_menu()` — ReplyKeyboard «📅 Сегодня», «⚖️ Взвеситься», «👤 Мой профиль», «❓ Помощь» (тексты кнопок — константы `texts.BTN_*`, обработчики фильтруют по ним).
  - `class Onboarding(StatesGroup)`: `sex, age, height, weight, activity, goal, target`
  - `async finish_onboarding(user: User, profile: Profile, deps: Deps) -> str` в `handlers/onboarding.py` — считает норму, ставит `start_weight_kg`, `goal_started_at`, `onboarded_at` (если пусто), `course_day` не трогает, сохраняет первое взвешивание, сбрасывает `milestones_sent`, возвращает текст итога (спец. 6.3).
  - `async start_onboarding(message, state)` — запуск анкеты с шага 1 (используется в meals/weight, когда анкеты нет).
  - `tests/fakes.py`: `MockedSession(BaseSession)` — запоминает все запросы (`requests: list[TelegramMethod]`), отвечает правдоподобными объектами (`SendMessage` / `SendPhoto` → `Message` с растущим `message_id`, `AnswerCallbackQuery` → `True`, `EditMessageText` → `Message`), умеет вернуть исключение для заданного метода (для 403 в Task 11); `FixedClock(dt)` с методом `advance(timedelta)`; помощники `make_message(text=None, photo=None, document=None, media_group_id=None, user_id=1)` и `make_callback(data, user_id=1)`; фикстура `bot_env` → `(dp, bot, session, deps)` на `MemoryStorage` + aiogram `MemoryStorage` для FSM.
  - Колбэки (`keyboards.py`): `consent`, `sex:{male|female}`, `act:{activity}`, `goal:{goal}`.

- [ ] **Step 1: Падающие тесты `tests/test_onboarding.py`:**
  - `/start` → текст содержит «без запретов и без голода», ссылку на политику (`texts.PRIVACY_URL`), кнопку `consent`; фото до согласия → снова просьба о согласии.
  - Полный проход эталонного примера (female, 30, 165, 70, moderate, lose, 62) → итог содержит «1 420», «2 201», «1 761», «Белки 126 г», «Жиры 63 г», «Углеводы 173 г», «2,1 л», «0,4 кг», «20 недел»; в хранилище: профиль, норма, `start_weight_kg == 70`, одно взвешивание 70, `consent_at` задан.
  - Неверный ввод: возраст «200», рост «абв», вес «20» → сообщение с диапазоном, состояние то же.
  - «70,5 кг» на шаге веса принимается как 70.5 (Review Focus 1).
  - «держать вес» → шаг 7 пропускается.
  - Желаемый вес при похудении ≥ текущего → просьба ввести меньше; при наборе ≤ текущего → больше.
  - Возраст 15 и цель «похудеть» → предложение «держать вес» и совет врача, цель `maintain`.
  - Желаемый вес с ИМТ < 18.5 → предупреждение и кнопки «Выбрать другой вес» / «Продолжить»; «Продолжить» завершает анкету.
  - Произвольный текст вне сценария после анкеты → текст `texts.FALLBACK`.
- [ ] **Step 2: Запустить** `pytest tests/test_onboarding.py -q` — FAIL.
- [ ] **Step 3: Реализовать** перечисленные файлы. Тексты спец. 6.1 и 6.3 — в `texts.py`; `PRIVACY_URL` берётся из переменной окружения `PRIVACY_URL` (добавить в `.env.example` в Task 12) с запасным значением-заглушкой `https://example.com/privacy`.
- [ ] **Step 4: Запустить** — PASS.
- [ ] **Step 5: Коммит** `"Сборка бота, согласие и анкета"`

---

### Task 9: Фото еды и дневник

**Files:**
- Create: `bot/handlers/meals.py`, `bot/handlers/diary.py`
- Modify: `bot/keyboards.py`, `bot/texts.py`, `bot/handlers/__init__.py`
- Test: `tests/test_meals.py`

**Interfaces:**
- Consumes: `Deps`, `FallbackVision`/`VisionUnavailable`, `prepare_image`, `choose_tip`, `meal_type_for`, `to_local`, `summarize`, `format_day`, `streak`.
- Produces:
  - Колбэки: `fat:{meal_id}:{none|little|much|sauce}` (0 / 10 / 20 г через `Analysis.with_fat`), `meal:edit:{meal_id}`, `meal:del:{meal_id}`, `meal:type:{meal_id}`, `mt:{meal_id}:{breakfast|lunch|snack|dinner}`.
  - `class MealEdit(StatesGroup)`: `correction`, `sauce` — в данных состояния `meal_id`.
  - `format_meal(analysis: Analysis, meal_type: MealType, remaining: float, tip: Tip | None) -> str` в `handlers/meals.py` — строки позиций «🍽 Название — N г — N ккал», скрытые с «(предположение)», «Итого», «✅ Записал в дневник: <тип>, N ккал», «Осталось на сегодня: …», подсказка последней строкой; `confidence == "low"` → строка «Не уверен — проверь и поправь, если нужно».
  - `meal_id = uuid4().hex`.

- [ ] **Step 1: Падающие тесты `tests/test_meals.py`** (пользователь после анкеты из фикстуры; `FakeVision`; `fetch_photo` возвращает байты маленького JPEG):
  - Фото в 13:00 МСК → сообщение «Анализирую тарелку…», затем результат; в хранилище обед с итогом-суммой позиций; ответ содержит остаток.
  - `ask_about_fat=True` → кнопки `fat:*`; `fat:<id>:little` пересчитывает запись (масло 10 г) и редактирует сообщение; `fat:<id>:sauce` → просьба описать соус, текст «майонез, ложка» уходит в `vision.correct`, запись обновлена.
  - `meal:edit` → текст «это гречка, 150 г» → `vision.correct(previous, text)`, запись обновлена.
  - `meal:del` → запись удалена; повторное нажатие → «Эта запись уже удалена» (Review Focus 4).
  - `meal:type` → выбор `mt:<id>:dinner` меняет тип.
  - `is_food=False` → текст «Не вижу еды», в дневнике пусто.
  - `VisionUnavailable(no_credits=True)` → пользователю «Сервис распознавания перегружен», админу (`admin_chat_id`) «Пополни баланс Kie»; второй случай через 10 минут — админу не отправляется.
  - `VisionUnavailable(no_credits=False)` → админу ничего.
  - Фото документом `image/jpeg` → обрабатывается как фото (Review Focus 2); документ `application/pdf` → `texts.FALLBACK`.
  - Альбом: два сообщения с одним `media_group_id` → один вызов `vision.analyze` и одна подсказка «Отправляй по одному фото…» (Review Focus 3).
  - Фото до анкеты → «Сначала посчитаем твою норму» и шаг 1 анкеты.
  - Перебор нормы: второй приём пищи переводит день за норму → текст спец. 8.5, `last_overeat_date` = сегодня; третий приём в тот же день → обычная подсказка, текста 8.5 нет.
  - Фото в 00:30 МСК (21:30 UTC) записывается на новую дату по Москве (Review Focus 5).
  - «📅 Сегодня» → текст `format_day` с правильной серией (приёмы вчера и сегодня → «🔥 2 дня подряд»).
  - Повторная доставка того же update не создаёт второй записи — проверяется в Task 11.
- [ ] **Step 2: Запустить** `pytest tests/test_meals.py -q` — FAIL.
- [ ] **Step 3: Реализовать.** Альбом: `deps.storage.mark_processed(f"album:{media_group_id}", now)`; `False` → подсказка только если ключ `album-hint:{media_group_id}` новый, иначе молчание. Уведомление админу: `alert_allowed("kie_no_credits", now, timedelta(hours=1))`.
- [ ] **Step 4: Запустить** — PASS.
- [ ] **Step 5: Коммит** `"Фото еды, уточнение масла, правки записей и дневник"`

---

### Task 10: Взвешивание, прогресс, график, поздравления, профиль

**Files:**
- Create: `bot/handlers/weight.py`, `bot/handlers/profile.py`
- Modify: `bot/keyboards.py`, `bot/texts.py`, `bot/handlers/__init__.py`
- Test: `tests/test_weight.py`

**Interfaces:**
- Consumes: `progress.*`, `calc.calculate_norm`, `render_weight_chart`, `finish_onboarding`, `start_onboarding`.
- Produces:
  - `class WeightInput(StatesGroup)`: `value`
  - `async apply_weighing(user: User, day: date, kg: float, deps: Deps) -> str` — сохраняет вес, пересчитывает норму по `trend_weight` (с `kcal_override = (прежняя норма + TDEE) / 2`, пока `today <= maintain_transition_until`), проверяет этапы и `drift`, возвращает текст ответа: шкала (спец. 6.7) или коридор для `maintain`, поздравление старшего нового этапа, иначе сообщение 9.4, если `weekly_up`; при `three_weeks` — текст и кнопка `norm:recalc`.
  - `format_progress(user: User, weighings: list[Weighing], today: date) -> str` — используется и в профиле.
  - Колбэки: `chart`, `goal_done:{maintain|new}`, `corridor:{back|keep}`, `norm:recalc`, `profile:goal`, `profile:restart`.
  - Тексты этапов: `texts.MILESTONES: dict[str, str]` по ключам `MILESTONE_ORDER`; `five_percent` упоминает пользу для здоровья.

- [ ] **Step 1: Падающие тесты `tests/test_weight.py`** (пользователь после эталонной анкеты, старт 70, цель 62; если не сказано иначе, взвешивание через 8 дней после анкеты, чтобы в окне тренда было только оно):
  - «⚖️ Взвеситься» → просьба ввести вес с напоминанием «утром, натощак»; «69» → шкала «Пройдено 1 из 8 кг» и поздравление `first_kg`; в хранилище `milestones_sent == {"first_kg"}`, норма пересчитана по весу 69 (ккал меньше прежних).
  - Повторное взвешивание в тот же день «68,8» заменяет значение (одна запись за день).
  - Скачок до 66.5 за раз → одно сообщение со старшим этапом `q25`, а в `milestones_sent` — `first_kg`, `five_percent`, `q25`.
  - Плюс > 0,3 кг к тренду неделю назад → текст «это вода и соль».
  - Три недели движения от цели → кнопка `norm:recalc`; нажатие пересчитывает норму.
  - Достижение 62 → поздравление со сводкой (недели, кг, дни с записями) и кнопки `goal_done:*`; `goal_done:maintain` → цель `maintain`, `target = тренд`, `maintain_transition_until = today + 14 дней`, ккал = (прежняя + TDEE) / 2; взвешивание через 15 дней → ккал = TDEE.
  - `goal_done:new` → шаги 6–7 анкеты, после них `start_weight_kg` = вес тренда, `milestones_sent` пуст.
  - Цель «держать»: тренд в пределах ±1,5 кг → «Вес держится в коридоре»; вне → кнопки `corridor:back|keep`.
  - `chart` при одном взвешивании → «График появится после второго взвешивания»; при двух — `SendPhoto`; исключение из `render_weight_chart` → «График временно недоступен» и текстовая шкала.
  - «⚖️ Взвеситься» до анкеты → запуск анкеты.
  - «👤 Мой профиль» → данные, норма, шкала, кнопки `chart`, `profile:goal`, `profile:restart`; `profile:restart` проходит анкету заново, `onboarded_at` и `course_day` не меняются.
  - Вес «7» → просьба ввести 30–300.
- [ ] **Step 2: Запустить** `pytest tests/test_weight.py -q` — FAIL.
- [ ] **Step 3: Реализовать.** «Сколько дней с записями» — `len(meal_days(user_id, goal_started_at))`.
- [ ] **Step 4: Запустить** — PASS.
- [ ] **Step 5: Коммит** `"Взвешивания, прогресс, график, поздравления и профиль"`

---

### Task 11: Точки входа Cloud Functions и рассылки

**Files:**
- Create: `bot/main.py`, `bot/daily.py`, `bot/course.py` (структура и карточки 1–3 как образец; остальные — Task 13), `bot/handlers/start.py` (дополнить `/stop_tips`, `/start_tips`, «❓ Помощь»)
- Test: `tests/test_main.py`, `tests/test_daily.py`

**Interfaces:**
- Consumes: `build_dispatcher`, `Deps`, `YdbStorage`, `YdbFsmStorage`, `connect`, `build_vision`.
- Produces:
  - `bot.main.handler(event: dict, context) -> dict` — Cloud Function. Проверяет заголовок `X-Telegram-Bot-Api-Secret-Token` == `WEBHOOK_SECRET` (иначе `{"statusCode": 403}`); тело — JSON (`isBase64Encoded` → декодировать); `mark_processed(f"update:{update_id}")` → повтор: `{"statusCode": 200}` без обработки; иначе `await dp.feed_update(bot, Update.model_validate(body))`; исключения логируются, ответ всё равно 200 (чтобы Telegram не повторял). Диспетчер, бот, пул YDB и `httpx.AsyncClient` создаются один раз на процесс (модульный кэш).
  - `async process_webhook(event: dict, dp: Dispatcher, bot: Bot, storage: Storage, secret: str, now: datetime) -> dict` — вся логика выше без глобальных объектов (её и тестируем).
  - `bot.course.CARDS: tuple[Card, ...]`, `@dataclass(frozen=True) Card(title: str, body: str, tip: str)`; `card_for(day: int) -> Card | None` (1-based).
  - `async run_daily(storage: Storage, bot: Bot, now: datetime) -> dict[str, int]` — по `daily_users()`: если `onboarded_at < today` и `card_for(course_day + 1)` есть → отправить карточку, `course_day += 1`; если понедельник (по Москве) и последнее взвешивание старше 7 дней → напоминание спец. 9.3; `TelegramForbiddenError` → `is_active = False`; пауза `asyncio.sleep(0.04)` между отправками; возвращает счётчики `cards`, `reminders`, `deactivated`.
  - `bot.daily.handler(event, context)` — Cloud Function таймера, вызывает `run_daily`.
  - Любое входящее сообщение от неактивного пользователя возвращает `is_active = True` — middleware в `app.py`.

- [ ] **Step 1: Падающие тесты `tests/test_main.py`:** неверный секрет → 403 и ни одного запроса к Telegram; одинаковый `update_id` дважды → одна обработка (одна запись приёма пищи с `FakeVision`); тело в base64 разбирается; исключение в обработчике → 200 и запись в лог.
- [ ] **Step 2: Падающие тесты `tests/test_daily.py`:** карточка 1 не уходит в день анкеты и уходит на следующий; `course_day` растёт; после последней существующей карточки — ничего; понедельник и взвешивание 8 дней назад → напоминание, 3 дня назад → нет; не понедельник → нет; `tips_enabled=False` → ничего; 403 от Telegram (`MockedSession` с исключением) → `is_active=False`, следующий запуск этого пользователя пропускает; сообщение от неактивного → снова активен. Плюс `/stop_tips` → `tips_enabled=False`, `/start_tips` → `True`. Плюс `test_cards_valid`: у каждой карточки заголовок, тело ≤ 600 знаков вместе с советом, нет запрещённых слов из спец. 5.
- [ ] **Step 3: Запустить** `pytest tests/test_main.py tests/test_daily.py -q` — FAIL.
- [ ] **Step 4: Реализовать.** Переменные окружения — спец. 17 и `PRIVACY_URL`.
- [ ] **Step 5: Запустить** `pytest -q` — PASS всего набора.
- [ ] **Step 6: Коммит** `"Вебхук, защита от повторов, ежедневные рассылки"`

---

### Task 12: Сборка, деплой и проверка точности

**Files:**
- Create: `scripts/build_zip.sh`, `scripts/create_tables.py`, `scripts/set_webhook.py`, `scripts/eval_vision.py`, `docs/deploy.md`, `.env.example`, `README.md`

**Interfaces:**
- Consumes: `create_tables`, `connect`, `build_vision`, `prepare_image`.
- Produces:
  - `scripts/build_zip.sh` → `dist/bot.zip` с `bot/` и `requirements.txt` (Cloud Functions ставит зависимости сам).
  - `scripts/create_tables.py` — `python -m scripts.create_tables` с `YDB_ENDPOINT`, `YDB_DATABASE`.
  - `scripts/set_webhook.py <url>` — `setWebhook` с `secret_token=WEBHOOK_SECRET`, `allowed_updates=["message","callback_query"]`, `drop_pending_updates=True`.
  - `scripts/eval_vision.py <папка>` — для каждой пары `name.jpg` + `name.json` (`{"kcal": ..., "protein": ..., "fat": ..., "carbs": ...}`) прогоняет каждую модель из аргументов `--models openai:gemini-3-pro claude:<id>`, печатает таблицу: модель, средняя абсолютная ошибка ккал, средняя ошибка в %, число сбоев, среднее время.
  - `docs/deploy.md` — пошагово для владельца без опыта разработки, команды `yc`: каталог; сервисный аккаунт с ролями `ydb.editor`, `functions.functionInvoker`, `serverless.functions.invoker`; бессерверная YDB; `create_tables`; функция `bot` (`python312`, точка входа `bot.main.handler`, 512 МБ, 120 с, переменные окружения, сервисный аккаунт); функция `daily` (`bot.daily.handler`, 256 МБ, 600 с); API Gateway со спецификацией OpenAPI (`POST /webhook` → функция `bot`); `set_webhook`; таймер-триггер `0 6 ? * * *` (06:00 UTC = 09:00 МСК) на `daily`; где смотреть логи; как обновить код; как пополнить Kie и сменить `VISION_PRIMARY`.
  - `README.md` — что это, как запустить тесты, ссылка на `docs/deploy.md` и спецификацию.

- [ ] **Step 1: Написать файлы.**
- [ ] **Step 2: Проверка сборки** `bash scripts/build_zip.sh && unzip -l dist/bot.zip | grep -E "bot/main.py|requirements.txt"` — Expected: обе строки найдены.
- [ ] **Step 3: Проверка скриптов без сети** `python scripts/eval_vision.py --help` и `python scripts/set_webhook.py --help` — Expected: справка без ошибок.
- [ ] **Step 4: Полный набор** `pytest -q` — PASS.
- [ ] **Step 5: Коммит** `"Сборка, инструкция по деплою, проверка точности моделей"`

Проверка на живом облаке (после того как владелец создаст ресурсы по `docs/deploy.md` и пополнит Kie): `/start` → анкета → фото → дневник в тестовом боте; ydb-тесты против облачной базы, если Task 7 шёл без Docker; `eval_vision.py` на 15–20 фото, выбор `VISION_PRIMARY` и `VISION_FALLBACK`.

---

### Task 13: Карточки «Знание дня» 4–14

**Files:**
- Modify: `bot/course.py`
- Test: `tests/test_daily.py::test_cards_valid` (уже есть)

- [ ] **Step 1: Написать карточки 4–14** по темам спец. 11, тон спец. 5, до 600 знаков, в конце практический совет. Утверждения — только общепринятые (белок насыщает сильнее углеводов и жиров; недосып повышает аппетит; колебания веса 1–1,5 кг в день — вода и соль); без цифр, которые нельзя подтвердить источником.
- [ ] **Step 2: Запустить** `pytest tests/test_daily.py -q` — PASS.
- [ ] **Step 3: Коммит** `"Карточки «Знание дня» 1–14"` и передать владельцу на вычитку.

### Task 14: Карточки «Знание дня» 15–30

То же, что Task 13, для тем 15–30. Коммит `"Карточки «Знание дня» 15–30"`. Может идти после запуска (спец. 11).
