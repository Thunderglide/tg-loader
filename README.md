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

В репозитории уже есть шаблон. Замените список `CHATS` на свои каналы:

```python
CHATS = [
    'itsysdes',              # username без @
    't.me/python_channel',   # ссылка
    -1001234567890,          # числовой ID группы/канала
]
```

### Ожидаемая структура

```text
tg-loader/
├── .env                 # создаёте сами (секреты)
├── .gitignore           # уже в репозитории
├── config.py            # список каналов
├── main.py
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

### Сброс скачанных данных

Удаляет базу и вложения, сессию не трогает:

```bash
python cleanup.py
python cleanup.py -y
```

Флаг `-y` пропускает подтверждение.
