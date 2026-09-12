# Список чатов для загрузки.
# Можно указывать username (без @), ссылку вида t.me/username, числовой ID (включая отрицательные для групп).
CHATS = [
    'itsysdes'
]

# Скачивать вложения при экспорте сообщений.
# False — только текст и метаданные; файлы можно докачать через download_files.py
DOWNLOAD_ATTACHMENTS = True

# Не скачивать файлы больше этого размера (в мегабайтах).
# None — без ограничения.
MAX_FILE_SIZE_MB = 50

# Расписание для scheduler.py (стандартный cron: мин час день месяц день_недели)
SCHEDULE_CRON = '0 */6 * * *'
SCHEDULE_TIMEZONE = 'Europe/Moscow'
# Сразу выполнить синхронизацию при запуске планировщика, затем ждать cron
SCHEDULE_RUN_ON_START = True
