# Бот подсчёта калорий по фото

Telegram-бот: пользователь фотографирует еду, бот распознаёт блюда (Kie.ai), считает ккал и БЖУ,
ведёт дневник, взвешивания и прогресс к цели. Тон — без запретов и без голода.
Работает в Яндекс Облаке: Cloud Functions + API Gateway (вебхук) + бессерверная YDB.

## Возможности

- Анкета и расчёт дневной нормы ккал, белков, жиров, углеводов и воды; профиль можно
  отправить одной строкой: «муж, 1987, 180, 87».
- Еда по фото, по тексту и по голосовому (Yandex SpeechKit): основная и запасная модель,
  уточнение масла и правка записей.
- ИИ-помощник: отвечает на вопросы по питанию и составляет рацион на день под норму;
  дневной лимит запросов — `AI_DAILY_LIMIT` (по умолчанию 50).
- Шаги и тренировки: потраченные ккал добавляются к норме дня.
- Еженедельный отчёт по понедельникам с советом модели; `/stop_tips` выключает напоминание
  о взвешивании, отчёт и вечернюю тему (`/start_tips` включает снова).
- Дневник дня: приёмы пищи, итоги, остаток, серия дней.
- Взвешивания, пересчёт нормы, еженедельное напоминание.
- Прогресс: шкала к цели, график веса с трендом и прогнозом, поздравления, «цель достигнута».
- Профиль и помощь. Фото нигде не сохраняются.

## Структура

```
index.py        точки входа Cloud Functions: index.handler (bot), index.daily (daily)
bot/            код бота (архив функции: index.py + bot/ + requirements.txt)
  main.py       точка входа функции bot (вебхук)
  daily.py      точка входа функции daily (таймер, рассылки)
  app.py        сборка диспетчера aiogram
  handlers/     обработчики: анкета, фото, текст, голос, дневник, взвешивание, активность, профиль
  calc.py, progress.py, chart.py, diary.py, vision.py, speech.py, weekly.py, assistant.py
  storage.py    интерфейс хранилища и MemoryStorage
  ydb_storage.py  хранилище на YDB, схема таблиц
  texts.py      все тексты сообщений
scripts/        сборка архива, таблицы, вебхук, проверка точности моделей
tests/          тесты (без сети и без YDB)
docs/deploy.md  пошаговый запуск в Яндекс Облаке
```

## Тесты

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest
```

Windows:

```
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt && .venv\Scripts\python -m pytest
```

Тесты против настоящей YDB помечены `ydb` и пропускаются, если не задан `YDB_ENDPOINT`.

## Скрипты

- `bash scripts/build_zip.sh` — собрать `dist/bot.zip` для Cloud Functions.
- `python -m scripts.create_tables [--print-yql]` — создать таблицы в YDB или напечатать YQL.
- `python scripts/set_webhook.py <url>` — привязать вебхук (`BOT_TOKEN`, `WEBHOOK_SECRET`).
- `python scripts/eval_vision.py <папка> --models openai:gemini-3-pro claude:<id>` — сравнить точность моделей.

Переменные окружения описаны в `.env.example`.

## Документы

- [Инструкция по запуску](docs/deploy.md)
- [Спецификация](docs/superpowers/specs/2026-10-24-calorie-bot-mvp-design.md)
- [Спецификация v1.1 (ИИ-помощник)](docs/superpowers/specs/2026-10-24-v1.1-ai-assistant-design.md)
- [План реализации](docs/superpowers/plans/2026-10-24-calorie-bot-mvp.md)
