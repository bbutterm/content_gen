# Тестирование

## Уровни подтверждения

| Проверка | Что устанавливает | Чего не устанавливает |
| --- | --- | --- |
| Review исходников/контракта | Параметры, пути, структура и ожидаемая семантика | Реальную доставку или состояние сервера |
| Python-регрессии | Настоящие App/SQLite/aiohttp/formatter и подменённые Telegram/provider-boundaries | Live Telegram и оплаченный provider-response |
| UI-fixtures в браузере | DOM, поля/запросы, dirty/stale protections, escaping и мобильную компоновку на синтетических данных | Работу production-route или настоящий QR-вход |
| Реальный локальный MP4 | Рендер/ffprobe, параметры, декодирование/движение в проверяемом тесте | Доставку этого ролика в Telegram |
| Health/API-guards | HTTP-доступность и ограничение неавторизованных запросов | Полную работоспособность всех подключений |
| Живое принятие владельцем | Конкретный QR-вход/paid-generation/публикацию в его окружении | Универсальную гарантию всех будущих отправок |

Не запускайте тесты с production-БД, скопированной `.env`, реальными сессиями или ключами. По возможности используйте отдельный source-only checkout. Не импортируйте `main.py` для запуска службы из тестового harness и не создавайте writable App над живыми данными.

## Подготовка и Python-suite

Из корня репозитория, после установки закреплённых зависимостей:

```bash
.venv/bin/python -I -m pip check
mkdir -p .test-tmp .test-output
chmod 700 .test-tmp .test-output
export TMPDIR="$PWD/.test-tmp"
# Explicit paths are supported by renderer tests, not by production .env.
export MOTION_TEST_SCRATCH="$TMPDIR"
export MOTION_TEST_FFMPEG="$(readlink -f "$(command -v ffmpeg)")"
export MOTION_TEST_FFPROBE="$(readlink -f "$(command -v ffprobe)")"
test -x "$MOTION_TEST_FFMPEG"
test -x "$MOTION_TEST_FFPROBE"
test -r /usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -B tests/run_tests.py
```

`tests/run_tests.py` объединяет `Acceptance` и discovery `tests/regression/test_*.py` и проверяет импорт текущих модулей. Тесты должны использовать пути checkout и стандартный временный каталог, не пути старой staging-машины. `MOTION_TEST_*` задают известные тестовые overrides. Если suite обращается к отсутствующему личному каталогу или импортирует старую сборку, это дефект переносимости тестов, а не необходимость создать чужую структуру директорий.

Закреплённые зависимости и нужные бинарники должны быть действительно установлены. Пропущенный renderer-тест или запуск под старым импортированным кодом не считается подтверждением нового релиза. Не приводите фиксированный счётчик «все N тестов» без результата именно текущего suite.

Ожидаемые области suite: миграция/изоляция workspace, API session/header guards, reader QR-state, модерация/cleanup, очереди/медиа, расходные отчёты, formatting с Telethon entities, reanalysis/reset, news-outlook и независимый контент-план, ограничения и настоящее видео. Подмена transport тестирует локальную логику, не удаляет чужие Telegram-сообщения и не списывает деньги у провайдера.

Сфокусированный прогон, который не подменяет полный suite:

```bash
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -B -m unittest \
  discover -s tests/regression -p 'test_outlook*.py' -v
```

## JavaScript и реальный браузер

`tests/ui_*.js` — сценарии для **DOM браузера**, не самостоятельные Node-программы. `node --check` проверяет только синтаксис. Встроенный JavaScript проверяйте из **текущего** `static/index.html`, не только из устаревшего extracted-fixture:

```bash
.venv/bin/python -B - <<'PY'
import re
from pathlib import Path
source = Path('static/index.html').read_text()
scripts = re.findall(r'<script\b[^>]*>([\s\S]*?)</script>', source, re.I)
Path('.test-output/current-inline.js').write_text('\n'.join(scripts))
PY
node --check .test-output/current-inline.js
```

Fixture базовой панели:

```bash
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -B tests/serve_fixture.py
```

Она слушает только `127.0.0.1:18139`, страница `/fixture` использует синтетическую SQLite и mocked HTTP SDK. Откройте страницу в локальном браузере, запускайте соответствующие `tests/ui_acceptance.js`, `tests/ui_cleanup_tags_spending.js`, `tests/ui_formatting.js` через собственный browser-harness/DevTools. Смотрите реальный результат Promise/ошибки, не только наличие файла теста. Останавливайте fixture после проверки.

Для контент-плана есть `tests/serve_content_plan_fixture.py` на `127.0.0.1:18149/fixture` и `tests/ui_content_plan.js`. UI-хранилище/API синтетические; проверка inline-видео требует локального файла `tests/content_plan_ui_synthetic.mp4`, который **не хранится в Git**. Его можно подготовить без сети и LLM из тестового storyboard:

```bash
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -B - <<'PY'
import asyncio
import importlib.util
import os
import shutil
import tempfile
from pathlib import Path
from motion_renderer import render_motion
spec = importlib.util.spec_from_file_location(
    'motion_fixture', Path('tests/regression/test_motion_renderer.py')
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(dir=os.environ['MOTION_TEST_SCRATCH']) as folder:
    output = Path(folder) / 'fixture.mp4'
    asyncio.run(render_motion(module.DEMO, output,
        ffmpeg_path=os.environ['MOTION_TEST_FFMPEG'],
        ffprobe_path=os.environ['MOTION_TEST_FFPROBE']))
    shutil.copyfile(output, 'tests/content_plan_ui_synthetic.mp4')
    Path('tests/content_plan_ui_synthetic.mp4').chmod(0o600)
PY
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -B tests/serve_content_plan_fixture.py
```

Не запускайте оба UI-сценария поверх одного уже изменённого fixture-state без перезагрузки/перезапуска. Если добавлен отдельный forecast-fixture, используйте его согласованный harness; не направляйте синтетические тесты на production-API.

Минимальная браузерная проверка новых прогнозных полей: desktop и узкий экран; four-key save в выбранную область, независимые переключатели/значения, default новой области, сохранение несохранённых полей при refresh, защита от старого save-response, предупреждение при смене области, escaping фокуса. Save не должен запускать генерацию. Обычные формы подключения/публикации должны остаться доступны.

## MP4 и сервисный UID

Тесты рендера проверяют настоящую генерацию, метаданные ffprobe, размер/права, различие кадров внутри сцены, таймаут/отмену, схему, небезопасные пути и сохранение прежнего файла при неудаче. Для отдельного вручную созданного синтетического артефакта:

```bash
ffprobe -v error -show_streams -show_format -of json \
  tests/content_plan_ui_synthetic.mp4
ffmpeg -v error -i tests/content_plan_ui_synthetic.mp4 -f null -
```

Первое проверяет метаданные, второе — полное декодирование. Разрешение/fps/codec и движение должны совпасть с [контрактом renderer](architecture.md); статичная картинка, завернутая в MP4, не считается проверкой анимации.

Проверка от root не заменяет запуск под непривилегированным UID с ожидаемыми `ProtectHome/ProtectSystem`, владельцами каталогов, бинарниками и шрифтом вне home. Service-UID probe должен использовать временные synthetic-данные, не действующую SQLite. Не расширяйте права исходников ради прохождения теста.

## Приёмка, которую документация не выполняет

Перед реальным генеративным действием отдельно подтвердите подключение, цену/возможные расходы, нужный workspace и сохранённый канал. QR-login, реальный completion, удаление management-карточки и actual Telegram text/video-send проверяются владельцем явно. Публикация исходников или новый healthy-process не закрывают эти acceptance-гейты автоматически.
