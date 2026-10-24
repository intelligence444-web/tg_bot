# Запуск бота в Яндекс Облаке

Инструкция для человека без опыта программирования. Всё делается в браузере, в консоли
Яндекс Облака: [console.yandex.cloud](https://console.yandex.cloud). Устанавливать на компьютер
ничего не нужно. Под каждым шагом для справки есть команда `yc` (консольная утилита Облака) —
её можно не выполнять, если вы сделали шаг мышкой.

Порядок важен: шаги идут так, что каждый следующий использует результат предыдущего.
Заведите себе заметку (в блокноте или Telegram «Избранное»), куда будете записывать значения,
которые встретятся по пути: токен бота, ID функции и т. п. Эту заметку никому не пересылайте:
там будут секреты.

Что получится в конце:

```
Пользователь → Telegram → API Gateway → функция bot ─┬─→ база YDB
                                                     └─→ Kie.ai (распознавание фото)
Таймер (каждый день в 09:00 МСК) → функция daily → база YDB → Telegram
```

---

## Шаг 0. Подготовьте значения (секреты)

Понадобятся шесть значений. Запишите их в заметку.

1. **BOT_TOKEN — токен бота.**
   1. Откройте в Telegram [@BotFather](https://t.me/BotFather) и нажмите «Запустить».
   2. Отправьте `/newbot`, затем имя бота (как его увидят люди) и короткое имя латиницей,
      которое оканчивается на `bot` (например, `my_calorie_test_bot`).
   3. BotFather пришлёт сообщение с токеном вида `123456789:AAH...`. Это и есть `BOT_TOKEN`.
   4. Для закрытого теста удобно сделать отдельного тестового бота, а основного — позже.
2. **ADMIN_CHAT_ID — ваш числовой ID в Telegram.** Сюда бот пришлёт «Пополни баланс Kie»,
   если закончатся кредиты.
   1. Откройте [@userinfobot](https://t.me/userinfobot) и нажмите «Запустить».
   2. Он ответит сообщением, где есть `Id: 123456789`. Число — это `ADMIN_CHAT_ID`.
   3. Обязательно откройте **своего** бота и нажмите «Запустить» (`/start`): Telegram не даёт
      ботам писать тем, кто им ни разу не писал.
3. **WEBHOOK_SECRET — секрет вебхука.** Любая случайная строка из 32 и более символов:
   латинские буквы, цифры, `_` и `-` (без пробелов и других знаков). Например, наберите
   «вслепую» по клавиатуре или воспользуйтесь любым генератором паролей, отключив спецсимволы.
   Пример формата (не используйте его): `k7Pq2_xZm9-Wd4Rt8Lb3Nf6Hs1Vy5Jc0`.
4. **KIE_API_KEY — ключ Kie.ai.** Войдите в аккаунт на [kie.ai](https://kie.ai), откройте
   раздел **API Key** и скопируйте ключ. Там же пополняется баланс: без кредитов бот не сможет
   распознавать фото (ответит «Сервис распознавания перегружен…»).
5. **PRIVACY_URL — ссылка на политику обработки персональных данных.** У вас на лендинге уже есть
   `privacy.html`: откройте эту страницу в браузере и скопируйте адрес из адресной строки
   (например, `https://ваш-сайт.ru/privacy.html`). Бот покажет эту ссылку в `/start`.
6. **VISION_PRIMARY и VISION_FALLBACK — модели распознавания.** Пока не проверена точность
   (см. шаг 13), используйте:
   - `VISION_PRIMARY` = `openai:gemini-3-pro`
   - `VISION_FALLBACK` = `openai:gemini-2.5-pro`

---

## Шаг 1. Каталог

Каталог — это «папка» в Облаке, где будут лежать все ресурсы бота.

1. Откройте [console.yandex.cloud](https://console.yandex.cloud) и войдите. Если Облако
   создаётся впервые, подключите платёжный аккаунт (без него сервисы не запустятся;
   у новых аккаунтов обычно есть стартовый грант).
2. На главной странице консоли слева вверху выберите облако, затем нажмите
   **«Создать каталог»**.
3. Имя: `calorie-bot`. Галочку «Создать сеть по умолчанию» можно оставить. Нажмите **«Создать»**.
4. Дальше все шаги выполняйте внутри этого каталога (его имя видно в верхней строке консоли).

```
yc resource-manager folder create --name calorie-bot
yc config set folder-name calorie-bot
```

---

## Шаг 2. Сервисный аккаунт

Сервисный аккаунт — «робот», от имени которого функции ходят в базу, а API Gateway и таймер
вызывают функции.

1. В каталоге `calorie-bot` в строке поиска сверху наберите **«Identity and Access Management»**
   (или в левом меню **«Все сервисы» → «Identity and Access Management»**).
2. Слева выберите **«Сервисные аккаунты»** → **«Создать сервисный аккаунт»**.
3. Имя: `bot-sa`.
4. Нажмите **«Добавить роль»** и добавьте по очереди три роли:
   - `ydb.editor`
   - `functions.functionInvoker`
   - `serverless.functions.invoker`
5. Нажмите **«Создать»**.
6. Откройте созданный аккаунт `bot-sa` и запишите в заметку его **Идентификатор**
   (строка вида `aje...`) — это `ID_СЕРВИСНОГО_АККАУНТА`.

```
yc iam service-account create --name bot-sa
FOLDER_ID=$(yc config get folder-id)
SA_ID=$(yc iam service-account get bot-sa --format json | jq -r .id)
for ROLE in ydb.editor functions.functionInvoker serverless.functions.invoker; do
  yc resource-manager folder add-access-binding "$FOLDER_ID" --role "$ROLE" --subject "serviceAccount:$SA_ID"
done
```

---

## Шаг 3. База данных YDB (бессерверная)

1. В поиске консоли наберите **«Managed Service for YDB»** и откройте сервис.
2. Нажмите **«Создать базу данных»**.
3. Имя: `calorie-bot-db`. Тип базы: **Serverless** (бессерверная). Остальное не меняйте.
   Нажмите **«Создать базу данных»** и подождите, пока статус станет **Running** (1–2 минуты).
4. Откройте базу. На вкладке **«Обзор»** найдите раздел **«Соединение»** и строку
   **«Эндпоинт»** вида

   ```
   grpcs://ydb.serverless.yandexcloud.net:2135/?database=/ru-central1/b1g.../etn...
   ```

   Разделите её на две части и запишите:
   - `YDB_ENDPOINT` — всё **до** `/?database=`: `grpcs://ydb.serverless.yandexcloud.net:2135`
   - `YDB_DATABASE` — всё **после** `database=`: `/ru-central1/b1g.../etn...`

   (Если в консоли отдельно показано поле **«База данных»**, это и есть `YDB_DATABASE`.)

```
yc ydb database create --name calorie-bot-db --serverless
yc ydb database get calorie-bot-db
```

---

## Шаг 4. Таблицы

Таблицы создаются один раз SQL-запросом прямо в консоли.

1. Откройте базу `calorie-bot-db` и перейдите на вкладку **«Навигация»**.
2. Нажмите **«Новый SQL-запрос»**.
3. Скопируйте **весь** текст ниже целиком и вставьте в окно запроса.
4. Нажмите **«Выполнить»**. Должно появиться сообщение об успешном выполнении, а слева —
   шесть таблиц: `users`, `meals`, `weights`, `fsm`, `processed_updates`, `alerts`.
   Запрос безопасно выполнить повторно: существующие таблицы он не трогает.

```sql
CREATE TABLE IF NOT EXISTS `users` (
    user_id Int64 NOT NULL,
    consent_at Timestamp,
    profile Json,
    norm Json,
    start_weight_kg Double,
    goal_started_at Date,
    onboarded_at Date,
    maintain_transition_until Date,
    transition_from_kcal Double,
    milestones_sent Json,
    course_day Int32,
    tips_enabled Bool,
    is_active Bool,
    last_overeat_date Date,
    last_tip_variants Json,
    updated_at Timestamp,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS `meals` (
    user_id Int64 NOT NULL,
    meal_id Utf8 NOT NULL,
    eaten_at Timestamp,
    local_date Date,
    meal_type Utf8,
    analysis Json,
    kcal Double,
    fat_answer Utf8,
    PRIMARY KEY (user_id, meal_id)
);

CREATE TABLE IF NOT EXISTS `weights` (
    user_id Int64 NOT NULL,
    local_date Date NOT NULL,
    weight_kg Double,
    created_at Timestamp,
    PRIMARY KEY (user_id, local_date)
);

CREATE TABLE IF NOT EXISTS `fsm` (
    key Utf8 NOT NULL,
    state Utf8,
    data_json Json,
    PRIMARY KEY (key)
);

CREATE TABLE IF NOT EXISTS `processed_updates` (
    key Utf8 NOT NULL,
    created_at Timestamp,
    PRIMARY KEY (key)
)
WITH (TTL = Interval("P1D") ON created_at);

CREATE TABLE IF NOT EXISTS `alerts` (
    kind Utf8 NOT NULL,
    last_sent_at Timestamp,
    PRIMARY KEY (kind)
);
```

В свежей таблице `users` уже есть колонка `transition_from_kcal` (плавный выход на
поддержание веса), отдельно добавлять её не нужно.

Тот же текст печатает команда `python -m scripts.create_tables --print-yql`, а
`python -m scripts.create_tables` создаёт таблицы сама (нужен Python и переменные
`YDB_ENDPOINT`, `YDB_DATABASE`, а также доступ к базе, например `YDB_ACCESS_TOKEN_CREDENTIALS`
с IAM-токеном из `yc iam create-token`). Это вариант для разработчика, вам он не нужен.

---

## Шаг 5. Архив с кодом

Функциям нужен ZIP-архив, в корне которого лежат папка `bot` и файл `requirements.txt` —
и больше ничего (без папок `tests`, `.venv`, `docs`). Облако само установит библиотеки
по `requirements.txt`.

- **Проще всего:** попросите разработчика прислать готовый `bot.zip`.
- **Mac:** откройте «Терминал», перейдите в папку проекта и выполните
  `bash scripts/build_zip.sh` — архив появится в `dist/bot.zip`.
- **Windows:** откройте папку проекта в Проводнике, выделите **папку `bot`** и **файл
  `requirements.txt`** (с зажатой Ctrl), нажмите правой кнопкой →
  **«Отправить» → «Сжатая ZIP-папка»** (в Windows 11: **«Сжать в ZIP-файл»**).
  Важно выделить именно эти два элемента, а не папку проекта целиком.

---

## Шаг 6. Функция `bot`

1. В поиске консоли наберите **«Cloud Functions»** и откройте сервис.
2. Нажмите **«Создать функцию»**, имя `bot`, нажмите **«Создать»**.
3. Откроется **«Редактор»**. Выберите среду выполнения **Python 3.12** (`python312`).
   Если предложат шаблон с примером кода — снимите галочку «Добавить файлы с примерами кода»
   и нажмите **«Продолжить»**.
4. **Способ:** выберите **«ZIP-архив»** и загрузите `bot.zip` из шага 5.
5. **Точка входа:** `bot.main.handler`
6. **Параметры:**
   - Таймаут: `120` секунд
   - Память: `512 МБ`
   - Сервисный аккаунт: `bot-sa`
7. **Переменные окружения** — нажмите «Добавить переменную окружения» для каждой строки:

   | Ключ | Значение |
   |---|---|
   | `BOT_TOKEN` | из шага 0 |
   | `KIE_API_KEY` | из шага 0 |
   | `WEBHOOK_SECRET` | из шага 0 |
   | `ADMIN_CHAT_ID` | из шага 0 |
   | `PRIVACY_URL` | из шага 0 |
   | `VISION_PRIMARY` | `openai:gemini-3-pro` |
   | `VISION_FALLBACK` | `openai:gemini-2.5-pro` |
   | `YDB_ENDPOINT` | из шага 3 |
   | `YDB_DATABASE` | из шага 3 |
   | `YDB_METADATA_CREDENTIALS` | `1` |

8. Нажмите **«Сохранить изменения»** и дождитесь, пока версия соберётся (статус «Активна»;
   первая сборка может занять пару минут, потому что ставятся библиотеки).
   Если сборка упала, ошибка будет видна там же — пришлите её текст разработчику.
9. Откройте вкладку **«Обзор»** функции и запишите **Идентификатор** (строка вида `d4e...`) —
   это `ID_ФУНКЦИИ_BOT`. Функцию **не** делайте публичной: её вызывает API Gateway от имени
   сервисного аккаунта.

```
yc serverless function create --name bot
yc serverless function version create \
  --function-name bot \
  --runtime python312 \
  --entrypoint bot.main.handler \
  --memory 512m \
  --execution-timeout 120s \
  --service-account-id "$SA_ID" \
  --source-path dist/bot.zip \
  --environment BOT_TOKEN=...,KIE_API_KEY=...,WEBHOOK_SECRET=...,ADMIN_CHAT_ID=...,PRIVACY_URL=...,VISION_PRIMARY=openai:gemini-3-pro,VISION_FALLBACK=openai:gemini-2.5-pro,YDB_ENDPOINT=...,YDB_DATABASE=...,YDB_METADATA_CREDENTIALS=1
```

---

## Шаг 7. Функция `daily` (ежедневные рассылки)

Тот же архив, другая точка входа.

1. **Cloud Functions → «Создать функцию»**, имя `daily` → **«Создать»**.
2. Среда выполнения **Python 3.12**, способ **«ZIP-архив»**, тот же `bot.zip`.
3. **Точка входа:** `bot.daily.handler`
4. **Таймаут:** `600` секунд, **память:** `256 МБ`, **сервисный аккаунт:** `bot-sa`.
5. **Переменные окружения:**

   | Ключ | Значение |
   |---|---|
   | `BOT_TOKEN` | из шага 0 |
   | `YDB_ENDPOINT` | из шага 3 |
   | `YDB_DATABASE` | из шага 3 |
   | `YDB_METADATA_CREDENTIALS` | `1` |

6. **«Сохранить изменения»**.

```
yc serverless function create --name daily
yc serverless function version create \
  --function-name daily \
  --runtime python312 \
  --entrypoint bot.daily.handler \
  --memory 256m \
  --execution-timeout 600s \
  --service-account-id "$SA_ID" \
  --source-path dist/bot.zip \
  --environment BOT_TOKEN=...,YDB_ENDPOINT=...,YDB_DATABASE=...,YDB_METADATA_CREDENTIALS=1
```

---

## Шаг 8. API Gateway (адрес для Telegram)

1. В поиске консоли наберите **«API Gateway»** → **«Создать API-шлюз»**.
2. Имя: `bot-gateway`.
3. В поле **«Спецификация»** сотрите пример и вставьте текст ниже, заменив две строки
   в угловых скобках на значения из заметки: `<ID_ФУНКЦИИ_BOT>` (шаг 6) и
   `<ID_СЕРВИСНОГО_АККАУНТА>` (шаг 2). Угловые скобки тоже уберите. Отступы (пробелы в начале
   строк) сохраните как есть.

```yaml
openapi: 3.0.0
info:
  title: calorie-bot
  version: 1.0.0
paths:
  /webhook:
    post:
      summary: Вебхук Telegram
      operationId: webhook
      x-yc-apigateway-integration:
        type: cloud_functions
        function_id: <ID_ФУНКЦИИ_BOT>
        service_account_id: <ID_СЕРВИСНОГО_АККАУНТА>
      responses:
        '200':
          description: OK
```

4. Нажмите **«Создать»**. Откройте шлюз и запишите **«Служебный домен»** — адрес вида
   `https://d5d....apigw.yandexcloud.net`. Это `API_GATEWAY_URL`.

Этот же текст лежит в файле `docs/api-gateway.yaml`.

```
yc serverless api-gateway create --name bot-gateway --spec docs/api-gateway.yaml
```

---

## Шаг 9. Вебхук (связать Telegram с API Gateway)

1. Соберите в заметке ссылку, подставив свои значения вместо `<TOKEN>`, `<API_GATEWAY_URL>`
   и `<SECRET>` (угловые скобки убрать):

   ```
   https://api.telegram.org/bot<TOKEN>/setWebhook?url=<API_GATEWAY_URL>/webhook&secret_token=<SECRET>&drop_pending_updates=true&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D
   ```

   Например (значения выдуманные):

   ```
   https://api.telegram.org/bot123456789:AAHxxxx/setWebhook?url=https://d5dabc123.apigw.yandexcloud.net/webhook&secret_token=k7Pq2_xZm9-Wd4Rt8Lb3Nf6Hs1Vy5Jc0&drop_pending_updates=true&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D
   ```

   Хвост `%5B%22message%22%2C%22callback_query%22%5D` — это закодированный список
   `["message","callback_query"]`, копируйте его как есть.
2. Вставьте ссылку в адресную строку браузера и нажмите Enter.
3. Должен открыться ответ `{"ok":true,"result":true,"description":"Webhook was set"}`.
4. Проверить вебхук можно ссылкой `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`:
   в ответе должен быть ваш адрес `.../webhook`, а `last_error_message` — отсутствовать.

Вариант для разработчика: `BOT_TOKEN=... WEBHOOK_SECRET=... python scripts/set_webhook.py <API_GATEWAY_URL>/webhook`.

---

## Шаг 10. Таймер для ежедневных рассылок

Каждый день в 06:00 UTC (= 09:00 по Москве) таймер запускает функцию `daily`: карточка
«Знание дня» и напоминание о взвешивании по понедельникам.

1. **Cloud Functions** → слева **«Триггеры»** → **«Создать триггер»**.
2. Имя: `daily-timer`. Тип: **«Таймер»**.
3. **Cron-выражение:** `0 6 ? * * *`
4. **Функция:** `daily`, тег версии `$latest`, **сервисный аккаунт:** `bot-sa`.
5. **«Создать триггер»**.

```
yc serverless trigger create timer \
  --name daily-timer \
  --cron-expression '0 6 ? * * *' \
  --invoke-function-name daily \
  --invoke-function-service-account-name bot-sa
```

---

## Шаг 11. Проверка после запуска

Откройте своего бота в Telegram и пройдите по списку:

- [ ] `/start` — пришло приветствие со ссылкой на политику (ссылка открывается).
- [ ] Анкета — ответили на все вопросы, бот показал дневную норму ккал и БЖУ.
- [ ] Фото еды — бот распознал блюдо и показал ккал (первый ответ после паузы может идти
      10–20 секунд: функция «просыпается»).
- [ ] Кнопка **«📅 Сегодня»** (дневник) — приём пищи виден, итоги дня посчитаны.
- [ ] Взвешивание — кнопка **«⚖️ Взвеситься»**, ввели вес, бот принял его и показал прогресс.
- [ ] На следующий день в 09:00 по Москве пришла карточка «Знание дня».

Если бот молчит: проверьте `getWebhookInfo` (шаг 9) и логи (шаг 12).

---

## Шаг 12. Где смотреть логи

1. **Cloud Functions** → функция `bot` (или `daily`) → вкладка **«Логи»**.
2. Выберите период (например, «последний час»). Ошибки помечены уровнем `ERROR`, в каждой
   записи есть `user_id` и `error_kind` (тип ошибки). Фото и ответы модели в логи не пишутся.
3. Все логи каталога также есть в сервисе **«Cloud Logging»**.

```
yc serverless function logs bot --since 1h
yc serverless function logs daily --since 24h
```

---

## Шаг 13. Обновление кода и настроек

**Новый код.** Получите новый `bot.zip` (шаг 5) → **Cloud Functions** → функция `bot` →
**«Редактор»** → способ **«ZIP-архив»** → загрузите архив → **«Сохранить изменения»**.
Создастся новая версия, переменные окружения и настройки сохранятся. То же сделайте для
функции `daily` (код у них общий). Вебхук и таймер перенастраивать не нужно. Если
разработчик менял таблицы, он отдельно пришлёт SQL-запрос для шага 4.

```
yc serverless function version create --function-name bot --runtime python312 \
  --entrypoint bot.main.handler --memory 512m --execution-timeout 120s \
  --service-account-id "$SA_ID" --source-path dist/bot.zip \
  --environment ...   # те же переменные, что в шаге 6
```

**Сменить модель распознавания (VISION_PRIMARY / VISION_FALLBACK).** Функция `bot` →
**«Редактор»** → в переменных окружения поменяйте значение → **«Сохранить изменения»**.
Формат значения: `openai:<модель>` (например, `openai:gemini-3-pro`, `openai:gemini-2.5-pro`)
или `claude:<идентификатор модели Claude из документации Kie>`. `VISION_FALLBACK` можно
оставить пустым — тогда запасной модели нет.

**Пополнить Kie.** Личный кабинет [kie.ai](https://kie.ai) → раздел пополнения баланса.
Если кредиты закончились, бот сообщит об этом в чат администратора (`ADMIN_CHAT_ID`),
не чаще раза в час. После пополнения ничего перезапускать не нужно.

**Проверка точности моделей.** Когда Kie пополнен, стоит сравнить модели на 15–20 своих
фото блюд, для которых КБЖУ известен (блюдо взвешено и посчитано). Для каждого фото
`name.jpg` нужен файл `name.json` с эталоном:

```json
{"kcal": 520, "protein": 30, "fat": 18, "carbs": 55}
```

Проще всего собрать фото и эталоны в одну папку и передать разработчику — он запустит

```
KIE_API_KEY=... python scripts/eval_vision.py photos/ --models openai:gemini-3-pro openai:gemini-2.5-pro claude:<id>
```

Скрипт печатает таблицу: модель, средняя ошибка в ккал и в %, число сбоев, среднее время.
Лучшая модель ставится в `VISION_PRIMARY`, вторая — в `VISION_FALLBACK`. Каждое фото — это
платный запрос к Kie для каждой модели.

---

## Сколько это стоит

У Cloud Functions, API Gateway и бессерверной YDB есть бесплатные ежемесячные лимиты
(бесплатный уровень): на закрытом тесте с несколькими людьми расход обычно в них укладывается
или близок к нулю. Точные лимиты и цены меняются — смотрите актуальные на сайте Яндекс Облака
в разделе «Цены» у каждого сервиса и в **«Биллинг»** консоли (там же можно настроить
уведомление о расходах — «Бюджеты»). Основная переменная статья расходов — запросы к Kie.ai
(по одному-два на каждое фото); их стоимость смотрите в личном кабинете Kie.

---

## Шпаргалка: какие значения где используются

| Значение | Где взять | Куда вписать |
|---|---|---|
| `BOT_TOKEN` | @BotFather | функции `bot`, `daily`; ссылка вебхука |
| `ADMIN_CHAT_ID` | @userinfobot | функция `bot` |
| `WEBHOOK_SECRET` | придумать | функция `bot`; ссылка вебхука |
| `KIE_API_KEY` | kie.ai → API Key | функция `bot` |
| `PRIVACY_URL` | адрес privacy.html на лендинге | функция `bot` |
| `YDB_ENDPOINT`, `YDB_DATABASE` | база YDB → «Обзор» | функции `bot`, `daily` |
| `ID_СЕРВИСНОГО_АККАУНТА` | IAM → `bot-sa` | спецификация API Gateway |
| `ID_ФУНКЦИИ_BOT` | функция `bot` → «Обзор» | спецификация API Gateway |
| `API_GATEWAY_URL` | API Gateway → «Служебный домен» | ссылка вебхука |
