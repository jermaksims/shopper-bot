# shopper-bot

Собирает товары и цены (особенно скидки Black Friday) с сайтов одежды — Uniqlo, Ralph Lauren,
Tommy Hilfiger, Macy's и любых других — в SQLite, экспортирует в CSV/XLSX и отвечает на вопросы
вроде «самые дешёвые мужские свитера со скидкой больше 50%» через чат с Claude.

## Установка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[browser,dev]'
playwright install chrome      # или используйте уже установленный Google Chrome
export ANTHROPIC_API_KEY=...   # нужен только для `shopper chat`
```

## Использование

```bash
shopper scrape --store uniqlo --limit 50   # пробный запуск одного магазина
shopper scrape                             # все включённые магазины из config/stores.yaml
shopper scrape --workers 3                 # магазины параллельно (внутри магазина всегда последовательно)
shopper stats                              # сколько товаров, сколько со скидкой, статус блокировок
shopper top --max-price 50 --gender men    # быстрый топ скидок без LLM
shopper export deals.xlsx --only-deals     # таблица (CSV или XLSX)
shopper chat                               # вопросы к Claude на любом языке
```

Данные лежат в `data/shopper.db` (view `current_deals`), куки и профили браузера — в `data/`.

## Как обходятся блокировки (бесплатно)

Идея — не «взламывать» защиту, а выглядеть как обычный покупатель и брать данные оттуда же,
откуда их берёт сам сайт. Для каждой категории методы пробуются по очереди (`methods` в конфиге),
при блокировке — переход к следующему:

1. **`api`** — внутренний JSON-эндпоинт сайта (Uniqlo commerce API, SFCC `Search-UpdateGrid`
   у Ralph Lauren). Чистые данные без парсинга вёрстки.
2. **`http`** — страница категории через `curl_cffi` с TLS/HTTP2-отпечатком настоящего Chrome
   (обычный `requests` банят по отпечатку мгновенно) + JSON-LD / `__NEXT_DATA__` / встроенный state.
3. **`browser`** — настоящий Chrome через Playwright с постоянным профилем: сайт сам проходит
   JS-проверки, а мы перехватываем JSON-ответы, которые грузит страница.

Плюс поведение человека: прогрев с главной страницы, сохранённые куки, пауза 2–10 с со случайным
разбросом, экспоненциальный откат при 403/429, распознавание страниц-челленджей Akamai /
PerimeterX / Cloudflare / DataDome (`shopper/fetch/blocks.py`).

**Капчи бот не решает.** В режиме `headless: false` окно Chrome видно — если появилась проверка,
пройдите её руками один раз (бот ждёт 60 с), профиль запомнит куки. Иначе магазин помечается
`blocked`, и стоит повторить позже.

Запускайте с домашнего компьютера: IP дата-центров (облако, VPS) Akamai-сайты блокируют почти
всегда. Если всё же нужен прокси, в конфиге магазина есть поле `proxy`.

## Добавить или починить магазин

Ссылки на распродажи и id категорий меняются каждый сезон. **URL и id в `config/stores.yaml`
для Macy's, Tommy Hilfiger и Ralph Lauren — стартовые значения, их нужно сверить с сайтом перед
первым запуском.** Если магазин отдаёт 0 товаров:

1. Откройте страницу распродажи в Chrome → DevTools → Network → Fetch/XHR.
2. Прокрутите / нажмите «Load more» и найдите запрос, который возвращает товары
   (`products?`, `Search-UpdateGrid`, `/xapi/`, `graphql`…).
3. Впишите в конфиг:
   * сайт на Salesforce Commerce Cloud → `adapter: sfcc`, `grid_path`, а в категории `cgid`;
   * любой другой → `adapter: generic`, а в категории `xhr_match: "<часть URL запроса>"` —
     универсальный парсер (`shopper/parse/products.py`) сам найдёт в JSON название, ссылку,
     цену и старую цену.
4. `shopper scrape --store <имя> --limit 20 -v` и `shopper stats`.

Сохраните ответ сайта в `tests/fixtures/` и добавьте тест — так парсер не сломается незаметно.

## Чат с Claude

`shopper chat` не вставляет всю таблицу в промпт: Claude получает инструменты
`search_deals`, `compare_similar`, `get_price_history`, `db_overview` и `run_sql` (только
`SELECT`, база открыта read-only) и сам делает запросы к локальной базе. Модель по умолчанию —
`claude-opus-5-5` (переопределяется `SHOPPER_MODEL`). Подключён серверный fallback на случай
отказа модели (`fallbacks: "default"`).

Колонка `suspicious = 1` значит, что заявленную «старую» цену мы ни разу не видели за время
наблюдения — скидка может быть завышена. Чем дольше бот собирает историю цен, тем точнее эта
проверка.

## Структура

```
config/stores.yaml       магазины, методы, категории, задержки
shopper/fetch/           http.py (curl_cffi), browser.py (Playwright), blocks.py, cache.py
shopper/stores/          uniqlo.py, sfcc.py (Ralph Lauren, Tommy), macys.py, base.py (generic)
shopper/parse/products.py  JSON-LD / embedded state / универсальный поиск товаров в JSON
shopper/db.py            SQLite: products, price_history, scrape_runs, view current_deals
shopper/chat/            инструменты и цикл tool-use для Claude
tests/                   тесты на сохранённых ответах сайтов
```

Используйте для личных покупок: собирается только публичный каталог без логина, с низкой
частотой запросов. Соблюдайте условия использования сайтов.
