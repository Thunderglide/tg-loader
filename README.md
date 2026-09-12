# tg-loader

Скрипт выгружает историю сообщений из Telegram (каналы, группы, супергруппы) в локальную SQLite-базу и сохраняет вложения на диск.

## 1. Скачать проект

Нужны Git и Python 3.10+.

```bash
git clone https://github.com/Thunderglide/tg-loader.git
cd tg-loader
```

Либо скачайте ZIP с GitHub: **Code → Download ZIP**, распакуйте архив и перейдите в папку проекта.

## 2. Какие файлы добавить

`.gitignore` уже есть в репозитории — его создавать не нужно. Он скрывает секреты, сессию, базу и скачанные файлы.

После клонирования добавьте только `.env` и укажите каналы в `config.py`.

### `.env`

Создайте файл `.env` в корне проекта (рядом с `main.py`):

```env
API_ID=12345678
API_HASH=your_api_hash_here
PHONE=+79001234567
```

| Переменная | Обязательно | Описание |
|---|---|---|
| `API_ID` | да | Числовой id приложения с [my.telegram.org](https://my.telegram.org) → API development tools |
| `API_HASH` | да | Строковый hash того же приложения |
| `PHONE` | нет | Номер в формате `+79001234567`. Если не задан, скрипт спросит его при первом запуске |

Не коммитьте `.env` и не публикуйте ключи.

### `config.py`

В репозитории уже есть шаблон. Замените список `CHATS` на свои каналы и при необходимости настройте вложения и расписание:

```python
CHATS = [
    'itsysdes',              # username без @
    't.me/python_channel',   # ссылка
    -1001234567890,          # числовой ID группы/канала
]

DOWNLOAD_ATTACHMENTS = True   # False — история без файлов
MAX_FILE_SIZE_MB = 50         # None — без ограничения размера
SCHEDULE_CRON = '0 */6 * * *' # мин час день месяц день_недели
SCHEDULE_TIMEZONE = 'Europe/Moscow'
SCHEDULE_RUN_ON_START = True  # сразу синхронизация, затем по cron
```

| Параметр | Описание |
|---|---|
| `DOWNLOAD_ATTACHMENTS` | Скачивать файлы при экспорте. `False` — только сообщения и метаданные вложений |
| `MAX_FILE_SIZE_MB` | Не скачивать файлы больше этого размера (мегабайты). `None` — без лимита |
| `SCHEDULE_CRON` | Расписание для `scheduler.py` (стандартный cron из 5 полей) |
| `SCHEDULE_TIMEZONE` | Часовой пояс cron. Если не задан — локальная зона |
| `SCHEDULE_RUN_ON_START` | Сразу выполнить синхронизацию при запуске планировщика |

Метаданные вложений пишутся в базу всегда. Если файл пропущен (режим без вложений или слишком большой), его можно докачать позже через `download_files.py`.

### Ожидаемая структура

```text
tg-loader/
├── .env                 # создаёте сами (секреты)
├── .gitignore           # уже в репозитории
├── config.py            # каналы, лимит файлов, расписание
├── exporter.py          # общая логика экспорта
├── main.py
├── download_files.py    # докачка отсутствующих файлов
├── scheduler.py         # автозапуск по cron
├── cleanup.py
├── requirements.txt
├── venv/                # появится после шага 3
├── session.session      # появится после первого входа
├── data/                # появится при запуске
│   ├── telegram_export.db
│   └── files/
└── logs/
```

## 3. Виртуальное окружение

Команды выполняйте из корня проекта (`tg-loader`).

### Windows (cmd)

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Если активация в PowerShell запрещена, один раз выполните:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### macOS и Linux

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Признак, что окружение активно: в начале строки терминала появляется `(venv)`.

Выйти из окружения: `deactivate`.

## 4. Запуск: скачивание каналов

1. Активируйте `venv` (шаг 3).
2. Убедитесь, что заполнены `.env` и `config.py`.
3. Запустите экспорт:

```bash
python main.py
```

При первом запуске скрипт запросит номер телефона (если нет `PHONE`) и код подтверждения. Код приходит в **официальное приложение Telegram** (чат «Telegram» или уведомление), не в SMS. Не пересылайте этот код — после пересылки он сразу становится недействительным. Если включена 2FA, скрипт отдельно спросит пароль.

После успешного входа создаётся файл `session.session`. Повторный ввод кода не нужен, пока сессия на месте.

Сообщения пишутся в `data/telegram_export.db`, вложения — в `data/files/`. Повторный запуск докачивает только новые сообщения.

### Только выбранные каналы

Игнорирует список из `config.py` и загружает указанные чаты:

```bash
python main.py --chat itsysdes --chat t.me/another_channel
```

### Без вложений

Сохраняет сообщения, реакции и `raw_data`, файлы на диск не качает. В таблицу `attachments` всё равно пишется путь, имя и размер — докачать можно позже:

```bash
python main.py --no-attachments
```

То же самое через конфиг: `DOWNLOAD_ATTACHMENTS = False`.

### Лимит размера файла

Не скачивать файлы больше N мегабайт (переопределяет `MAX_FILE_SIZE_MB`):

```bash
python main.py --max-file-size 20
```

## 5. Докачка отсутствующих файлов

Скачивает файлы из таблицы `attachments`, которых нет на диске (пропущены по лимиту, режиму без вложений или удалены вручную). Уже лежащие файлы не трогает. По умолчанию уважает `MAX_FILE_SIZE_MB`.

```bash
python download_files.py
python download_files.py --chat itsysdes
python download_files.py --max-file-size 20
python download_files.py --force
```

`--force` игнорирует лимит размера.

## 6. Автозапуск по расписанию

Долгоживущий процесс. Нужна уже сохранённая `session.session` (сначала один раз войдите через `python main.py`). Расписание берётся из `SCHEDULE_CRON`:

```bash
python scheduler.py
```

Пример: `'0 */6 * * *'` — каждые 6 часов. Если `SCHEDULE_RUN_ON_START = True`, первая синхронизация выполняется сразу, дальше — по cron. Остановка: Ctrl+C.

Планировщик использует те же `CHATS`, `DOWNLOAD_ATTACHMENTS` и `MAX_FILE_SIZE_MB`, что и `main.py`.

## 7. Сброс скачанных данных

Удаляет базу и вложения, сессию не трогает:

```bash
python cleanup.py
python cleanup.py -y
```

Флаг `-y` пропускает подтверждение.
