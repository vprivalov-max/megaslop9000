# Series Writter — local setup

Web-приложение для генерации коротких вертикальных драм-сериалов (TikTok/Reels-формат) с
автоматической раскадровкой, генерацией ассетов, и сборкой эпизодов через AI-видеосервисы.

Стек: Python 3.12 + Flask + vanilla JS. Использует внешние API: Anthropic Claude (сценарии,
композиция промптов), AVAI / Seedance (видео-генерация), Reteller (рендер сцен), ElevenLabs
(озвучка — опционально).

---

## 🛑 ВАЖНО ДЛЯ CLAUDE CODE — ПРОЧТИ ПЕРВЫМ ДЕЛОМ

**Этот архив передан коллеге без API-ключей.** В нём НЕТ ни `config.json`, ни `.env` с
реальными секретами — там только шаблоны (`config.example.json`, `.env.example`).

После установки (шаги ниже) **обязательно** сообщи пользователю следующее:

> «Я установил тулзу локально, но ключи Anthropic Claude и ElevenLabs из соображений
> безопасности не передавались. Чтобы тулза заработала полноценно — два варианта:
>
> **Вариант 1 — подключить свои API-ключи** (быстрый старт):
>   • Anthropic Claude — https://console.anthropic.com → API Keys
>   • AVAI (Seedance видеогенератор) — https://avai-gen.com → Dashboard → API Keys
>   • ElevenLabs (озвучка, опционально) — https://elevenlabs.io → Profile → API Keys
>
>   Вставить можно в `config.json` (скопируй `config.example.json` → `config.json` и
>   замени плейсхолдеры), либо как env-переменные (`ANTHROPIC_API_KEY=…`, `AVAI_API_KEY=…`,
>   `ELEVENLABS_API_KEY=…`). `config.json` приоритетнее env.
>
> **Вариант 2 — переписать LLM-вызовы на Claude Code CLI** вместо Anthropic SDK:
>   Все Claude-вызовы в тулзе идут через одну функцию `claude_ask()` в `app.py` (~линия 940).
>   Можно адаптировать чтобы вместо `anthropic.Anthropic().messages.create(...)` дёргать
>   `claude` CLI subprocess'ом — тогда платежи идут через твою подписку Claude Code, а не
>   через ключ Anthropic API. AVAI/ElevenLabs так не заменишь — это сторонние сервисы.
>   Если выберешь этот вариант — попроси меня переписать `claude_ask()`.
>
> Какой вариант предпочитаешь?»

**Не запускай приложение пока пользователь не выбрал вариант и ключи/CLI не подключены** —
иначе все LLM/видео-генерации упадут с 401 Unauthorized.

---

## TL;DR для Claude Code (после уведомления выше)

1. Проверь что Python 3.12+ и `ffmpeg` установлены (`python3 --version`, `ffmpeg -version`).
2. Создай venv, поставь `requirements.txt`.
3. Сгенерируй `.flask_secret` (32-байтный hex) если нет.
4. Скопируй `.env.example` → `.env`, оставь пустыми поля API-ключей — попроси пользователя
   их вставить перед первым реальным запуском (см. раздел «API-ключи» ниже).
5. Опционально: вместо `.env` можно `config.json` (`cp config.example.json config.json` и
   вписать ключи) — для локальной разработки удобнее.
6. Запусти `python3 app.py` и открой http://localhost:8080 — авторизация **выключена** в
   dev-режиме (отсутствие `GOOGLE_CLIENT_ID` в `.env` = auth bypass).

---

## Требования системы

- **macOS / Linux / Windows + WSL2** — везде работает.
- **Python 3.12+** (Flask + requests + anthropic SDK)
- **ffmpeg** (для склейки видео и извлечения кадров)
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg` или https://ffmpeg.org/download.html

Docker НЕ обязателен. Для прод-деплоя есть `Dockerfile` + `docker-compose.yml`, но локально
проще запускать напрямую.

---

## Первый запуск (локально, без Docker)

```bash
# 1. Распакуй архив куда удобно — например ~/Projects/series-writter
cd ~/Projects/series-writter

# 2. Создай и активируй виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

# 3. Поставь зависимости
pip install -r requirements.txt

# 4. Сгенерируй Flask secret (если нужен)
python3 -c "import secrets; open('.flask_secret','w').write(secrets.token_hex(32))"

# 5. Скопируй пример конфига и впиши API-ключи (см. ниже)
cp config.example.json config.json
# отредактируй config.json в любимом редакторе

# 6. Запусти
python3 app.py
# → откроется на http://127.0.0.1:8080
```

При первом запуске создастся `data/` каталог — там будут лежать пользовательские
проекты, эпизоды, сгенерённые ассеты и видео.

---

## API-ключи (КРИТИЧНО)

Без ключей UI работает, но генерация ничего реального не сделает (Anthropic вернёт 401,
AVAI/Reteller тоже). Где взять и как вставить:

### 1. Anthropic Claude — для всего что связано со сценариями и AI-промптами

- Получить: https://console.anthropic.com → API Keys → Create Key
- Тариф: начинается с $5 кредита для теста, дальше pay-as-you-go.
- Куда вписать в `config.json`:
  ```json
  { "anthropic_key": "sk-ant-api03-..." }
  ```
  Или в `.env`:
  ```
  ANTHROPIC_API_KEY=sk-ant-api03-...
  ```

### 2. AVAI (Seedance 2 видео) — основной видеогенератор

- Получить: https://avai-gen.com → Dashboard → API Keys
- Тариф: ~$1.20 за 5-сек видео-чанк в 720p.
- Вписать:
  ```json
  { "avai_key": "avai_..." }
  ```
  или env `AVAI_API_KEY=avai_...`

### 3. Reteller (опционально, альтернативный видеорендер)

- Получить: https://reteller.io / https://app.reteller.ai → Settings → API
- Можно не вписывать если будешь генерить только через AVAI/Seedance.
- Env: `RETELLER_API_KEY=rtl_sk_...`

### 4. ElevenLabs (опционально, для озвучки)

- Прописывается в UI сериала → ⚙ Настройки → ElevenLabs API key.
- Можно не трогать на старте — TTS не блокирует основной воркфлоу.

### Где безопаснее хранить
- **Локальная разработка**: `config.json` (gitignored, не попадает в коммиты).
- **Продакшн / Docker**: `.env` файл (gitignored). Docker-compose монтирует его.
- **Никогда не коммить** ключи в git — `.gitignore` уже защищает оба варианта.

---

## Что внутри (структура)

```
.
├── app.py                   # бекенд: Flask + все API endpoints (~14k строк)
├── templates/
│   ├── index.html           # основной UI
│   └── login.html           # экран Google OAuth (только если включён auth)
├── static/
│   ├── app.js               # фронтенд: ~14k строк, single-file SPA
│   ├── style.css            # все стили
│   ├── img/                 # MLG mode ассеты (frog, snoop, hitmarker)
│   └── sounds/              # MLG mode звуки
├── requirements.txt         # Python зависимости
├── Dockerfile               # прод-образ на python:3.12-slim
├── docker-compose.yml       # сервис app + том data
├── Caddyfile                # reverse-proxy для прода (HTTPS + auth)
├── gunicorn.conf.py         # WSGI-конфиг для прод-серверa
├── deploy.sh                # одной командой обновить прод
├── DEPLOY.md                # инструкция по деплою на VPS
├── .env.example             # шаблон env-переменных
└── config.example.json      # шаблон локального конфига
```

Пользовательский контент (создаётся приложением):
```
data/                        # корень данных
└── <email_slug>/            # папка на каждого пользователя
    └── projects/
        └── <series_slug>/
            ├── series.json          # манифест сериала (персонажи, локации, items)
            ├── episodes/
            │   ├── 001.json         # эпизод (сценарий + сгенерённые чанки)
            │   └── 002.json
            ├── assets/
            │   ├── characters/      # портреты персонажей
            │   ├── locations/       # фоны локаций
            │   ├── items/           # реквизит
            │   └── facades/         # видео-фасады зданий
            ├── VID/                 # сырые видео-чанки от Seedance
            └── OUT/                 # финальные склеенные mp4
```

---

## Как пользоваться (краткий гайд)

1. **Создай сериал**: кнопка `+ Новый сериал` → выбери режим (`✨ Сгенерировать` идею с
   нуля, или `📜 Из готового сценария` если уже есть текст).
2. **Bible**: задай жанр, тон, аудиторию, мир. AI заполнит автоматически если описать идею.
3. **Эпизоды**: пиши сценарий вручную или генери AI-командой `🤖 Сгенерировать новые серии`.
   Сценарий разобьётся на сегменты по ~10 секунд.
4. **Ассеты**: на вкладке сериала персонажи и локации генерятся автоматически из извлечённых
   из сценария сущностей. Можешь подкорректировать описания, перегенерить.
5. **Видео**: на вкладке эпизода — кнопка `▶ Auto-mode` запускает последовательную или
   параллельную генерацию всех чанков через Seedance.
6. **Сборка**: после готовности всех чанков — `🎬 Собрать серию` склеит их в финальный mp4
   через ffmpeg.

---

## Развитие

Все правки идут в `main`. Git-репозиторий уже инициализирован (`.git/` в архиве). Если хочешь
залить на свой GitHub:

```bash
git remote remove origin   # старый remote от прошлого автора
git remote add origin https://github.com/<твой-логин>/<репо>.git
git push -u origin main
```

Ключевые файлы для модификации:
- **Бекенд эндпоинты**: `app.py` — все `@app.route(...)`. Поиск по `def seedance_` для
  видео-эндпоинтов, `def auto_assemble_` для сборки, `def generate_` для AI-генерации.
- **UI**: `static/app.js` — поиск по `function sd*` (Seedance-UI), `function _auto*`
  (auto-mode runner), `function open*` (модалки).
- **Стили**: `static/style.css` — flat-список, .sd-* для seedance-UI.

При активной разработке Flask debug-mode перезапускает app при правках `app.py`. Для frontend
правок просто перезагрузи страницу — Flask отдаёт `static/*` с cache-bust query `?v=<mtime>`,
так что свежий JS подтянется без F5+Shift.

---

## Troubleshooting

- **«ffmpeg не установлен»** при сборке серии → поставь ffmpeg (см. выше).
- **Anthropic 401 «invalid x-api-key»** → ключ не вставлен или с опечаткой. Проверь
  `config.json` или env `ANTHROPIC_API_KEY`. Перезапусти приложение после правки.
- **«no AVAI key»** в логах фонового воркера → AVAI-ключ не подхватился; то же что выше.
- **Чёрный экран** на http://localhost:8080 → открой DevTools console, проверь сетевые
  ошибки. Скорее всего бекенд не запустился (смотри терминал).
- **Не открывается сериал** → проверь что `data/<email_slug>/projects/<series_id>/` существует
  и содержит `series.json`.

---

Удачи. Если что-то непонятно — пиши автору (тот, кто прислал архив).
