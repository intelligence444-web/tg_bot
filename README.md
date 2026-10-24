# Бот подсчёта калорий по фото

Telegram-бот: пользователь фотографирует еду, бот распознаёт блюда (Kie.ai), считает ккал и БЖУ,
ведёт дневник, взвешивания и прогресс к цели. Тон — без запретов и без голода.
Работает в Яндекс Облаке: Cloud Functions + API Gateway (вебхук) + бессерверная YDB.

## Возможности

- Анкета и расчёт дневной нормы ккал, белков, жиров, углеводов и воды.
- Распознавание еды по фото с основной и запасной моделью, уточнение масла и правка записей.
- Дневник дня: приёмы пищи, итоги, остаток, серия дней.
- Взвешивания, пересчёт нормы, еженедельное напоминание.
- Прогресс: шкала к цели, график веса с трендом и прогнозом, поздравления, «цель достигнута».
- Подсказка после каждого фото и курс «Знание дня» (30 карточек, по одной в день).
- Профиль и помощь. Фото нигде не сохраняются.

## Структура

```
bot/            код бота (архив функции: bot/ + requirements.txt)
  main.py       точка входа функции bot (вебхук)
  daily.py      точка входа функции daily (таймер, рассылки)
  app.py        сборка диспетчера aiogram
  handlers/     обработчики: анкета, фото, дневник, взвешивание, профиль
  calc.py, progress.py, tips.py, chart.py, course.py, diary.py, vision.py
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
- [План реализации](docs/superpowers/plans/2026-10-24-calorie-bot-mvp.md)
