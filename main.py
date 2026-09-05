#!/usr/bin/env python3
"""
Telegram History Exporter
Скрипт для выгрузки истории сообщений из Telegram (каналы, группы, суперчаты с темами)
с сохранением сообщений и вложений в локальную SQLite базу и на диск.
"""
import os
import sys
import json
import argparse
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import (
    Message, MessageMedia, MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage, MessageMediaGame, MessageMediaInvoice,
    MessageMediaGeo, MessageMediaContact, MessageMediaDice,
    PeerChannel, PeerChat, PeerUser, ReactionCount, Reaction
)
from telethon.errors import RPCError, FloodWaitError
import aiosqlite

# Загрузка переменных окружения
load_dotenv()

API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
if not API_ID or not API_HASH:
    print("Ошибка: API_ID и API_HASH должны быть заданы в .env файле")
    sys.exit(1)

# Импорт конфигурации с чатами
try:
    from config import CHATS
except ImportError:
    print("Ошибка: файл config.py не найден или не содержит CHATS")
    sys.exit(1)

# Настройка логирования
LOG_DIR = Path('logs')
LOG_DIR.mkdir(exist_ok=True)
log_filename = LOG_DIR / f'telegram_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('TelegramExporter')

# Базовые пути
BASE_DIR = Path('data')
DB_PATH = BASE_DIR / 'telegram_export.db'
FILES_DIR = BASE_DIR / 'files'

# Размер пакета сообщений за один запрос
BATCH_SIZE = 100

# Создание необходимых папок
BASE_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------
# Класс для работы с базой данных
# ----------------------------------------------------------------------
class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self):
        """Создание таблиц, если их нет."""
        async with aiosqlite.connect(self.db_path) as db:
            # Таблица чатов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    title TEXT,
                    type TEXT,
                    username TEXT,
                    last_loaded_id BIGINT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Таблица сообщений
            await db.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    message_id BIGINT NOT NULL,
                    thread_id BIGINT,
                    author_id BIGINT,
                    author_name TEXT,
                    date TIMESTAMP,
                    text TEXT,
                    reply_to_msg_id BIGINT,
                    processed BOOLEAN DEFAULT FALSE,
                    raw_data JSON,
                    reactions JSON,
                    UNIQUE(chat_id, message_id)
                )
            ''')
            # Таблица вложений
            await db.execute('''
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    file_path TEXT NOT NULL,
                    file_name TEXT,
                    file_size INTEGER,
                    mime_type TEXT,
                    telegram_file_id TEXT
                )
            ''')
            # Индексы для скорости
            await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id)')
            await db.commit()

    async def get_chat_by_telegram_id(self, telegram_id: int):
        """Получить запись чата по telegram_id."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT id, telegram_id, title, type, username, last_loaded_id FROM chats WHERE telegram_id = ?',
                (telegram_id,)
            ) as cursor:
                return await cursor.fetchone()

    async def insert_chat(self, telegram_id: int, title: str, chat_type: str, username: str = None) -> int:
        """Вставить новый чат, возвращает его внутренний id."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'INSERT OR IGNORE INTO chats (telegram_id, title, type, username) VALUES (?, ?, ?, ?)',
                (telegram_id, title, chat_type, username)
            ) as cursor:
                await db.commit()
                # Получить id (существующий или новый)
                async with db.execute(
                    'SELECT id FROM chats WHERE telegram_id = ?', (telegram_id,)
                ) as c:
                    row = await c.fetchone()
                    return row[0] if row else None

    async def update_last_loaded_id(self, chat_id: int, last_loaded_id: int):
        """Обновить last_loaded_id для чата."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'UPDATE chats SET last_loaded_id = ? WHERE id = ?',
                (last_loaded_id, chat_id)
            )
            await db.commit()

    async def get_last_loaded_id(self, chat_id: int) -> int:
        """Получить последний загруженный message_id для чата."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT last_loaded_id FROM chats WHERE id = ?', (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def message_exists(self, chat_id: int, message_id: int) -> bool:
        """Проверить, существует ли сообщение в БД."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?',
                (chat_id, message_id)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None

    async def insert_message(self, chat_id: int, message_id: int, thread_id: Optional[int],
                             author_id: Optional[int], author_name: Optional[str],
                             date: datetime, text: Optional[str], reply_to_msg_id: Optional[int],
                             reactions_json: Optional[str], raw_data: Optional[str] = None):
        """Вставить сообщение, возвращает его внутренний id."""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    '''
                    INSERT OR IGNORE INTO messages
                    (chat_id, message_id, thread_id, author_id, author_name, date, text, reply_to_msg_id, reactions, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (chat_id, message_id, thread_id, author_id, author_name,
                     date.isoformat() if date else None, text, reply_to_msg_id,
                     reactions_json, raw_data)
                )
                await db.commit()
                # Получить id вставленной записи
                async with db.execute(
                    'SELECT id FROM messages WHERE chat_id = ? AND message_id = ?',
                    (chat_id, message_id)
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
            except Exception as e:
                logger.error(f"Ошибка вставки сообщения (chat_id={chat_id}, msg_id={message_id}): {e}")
                return None

    async def insert_attachment(self, message_db_id: int, file_path: str, file_name: str,
                                file_size: int, mime_type: str, telegram_file_id: str):
        """Вставить запись о вложении."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                '''
                INSERT INTO attachments
                (message_id, file_path, file_name, file_size, mime_type, telegram_file_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (message_db_id, file_path, file_name, file_size, mime_type, telegram_file_id)
            )
            await db.commit()


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------
def get_chat_type(chat) -> str:
    """Определить тип чата по объекту."""
    if hasattr(chat, 'megagroup') and chat.megagroup:
        return 'supergroup'
    if hasattr(chat, 'broadcast') and chat.broadcast:
        return 'channel'
    if hasattr(chat, 'group') and chat.group:
        return 'group'
    if hasattr(chat, 'is_channel') and chat.is_channel:
        return 'channel'
    return 'private'


def extract_author_info(message: Message) -> tuple:
    """Извлечь ID и имя автора."""
    author_id = None
    author_name = None
    if message.sender_id:
        author_id = message.sender_id
        if message.sender:
            if hasattr(message.sender, 'username'):
                author_name = f"@{message.sender.username}"
            if hasattr(message.sender, 'first_name'):
                name = message.sender.first_name
                if hasattr(message.sender, 'last_name') and message.sender.last_name:
                    name += f" {message.sender.last_name}"
                author_name = name if not author_name else f"{author_name} ({name})"
            if not author_name and hasattr(message.sender, 'title'):
                author_name = message.sender.title
    return author_id, author_name


def extract_reactions(message: Message) -> Optional[str]:
    """Извлечь реакции и сериализовать в JSON."""
    if not message.reactions:
        return None
    reactions_list = []
    if hasattr(message.reactions, 'results'):
        # Для каналов: ReactionCount
        for r in message.reactions.results:
            if r.count > 0:
                item = {'count': r.count}
                if hasattr(r, 'emoticon'):
                    item['emoticon'] = r.emoticon
                elif hasattr(r, 'custom_emoji_id'):
                    item['custom_emoji_id'] = r.custom_emoji_id
                reactions_list.append(item)
    elif hasattr(message.reactions, 'reactions'):
        # Для групп: список Reaction (содержат user_id и emoticon)
        for r in message.reactions.reactions:
            item = {'user_id': r.user_id}
            if hasattr(r, 'emoticon'):
                item['emoticon'] = r.emoticon
            elif hasattr(r, 'custom_emoji_id'):
                item['custom_emoji_id'] = r.custom_emoji_id
            reactions_list.append(item)
    return json.dumps(reactions_list, ensure_ascii=False) if reactions_list else None


async def download_media(message: Message, chat_telegram_id: int, message_id: int) -> Optional[List[Dict]]:
    """
    Скачать медиа из сообщения и сохранить в папку.
    Возвращает список словарей с информацией о скачанных файлах.
    """
    if not message.media:
        return None

    # Определяем, есть ли медиа, которое можно скачать
    media = message.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument,
                          MessageMediaWebPage, MessageMediaGame,
                          MessageMediaInvoice, MessageMediaGeo,
                          MessageMediaContact, MessageMediaDice)):
        # Скачиваем
        dir_path = FILES_DIR / str(chat_telegram_id) / str(message_id)
        dir_path.mkdir(parents=True, exist_ok=True)

        # Получаем имя файла
        file_name = None
        if hasattr(media, 'document') and media.document:
            for attr in media.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    file_name = attr.file_name
                    break
        elif hasattr(media, 'photo') and media.photo:
            # Для фото можно взять дату или сгенерировать имя
            file_name = f"photo_{message_id}.jpg"
        elif hasattr(media, 'webpage') and media.webpage:
            # Для веб-страниц не скачиваем
            return None
        else:
            # Генерируем имя по типу
            ext = 'bin'
            if hasattr(media, 'document') and media.document:
                mime = getattr(media.document, 'mime_type', '')
                if mime:
                    ext = mime.split('/')[-1] if '/' in mime else 'bin'
            elif hasattr(media, 'photo'):
                ext = 'jpg'
            file_name = f"file_{message_id}.{ext}"

        if not file_name:
            file_name = f"file_{message_id}.bin"

        file_path = dir_path / file_name

        # Проверяем, существует ли уже файл
        if file_path.exists():
            logger.info(f"Файл уже существует: {file_path}")
            file_size = file_path.stat().st_size
            mime_type = None
            if hasattr(media, 'document'):
                mime_type = getattr(media.document, 'mime_type', None)
            elif hasattr(media, 'photo'):
                mime_type = 'image/jpeg'
            telegram_file_id = None
            if hasattr(media, 'document'):
                telegram_file_id = media.document.id
            elif hasattr(media, 'photo'):
                telegram_file_id = media.photo.id
            return [{
                'file_path': str(file_path.relative_to(BASE_DIR)),
                'file_name': file_name,
                'file_size': file_size,
                'mime_type': mime_type,
                'telegram_file_id': str(telegram_file_id) if telegram_file_id else None
            }]

        # Скачиваем
        try:
            # Используем download_media с указанием пути
            downloaded_path = await message.download_media(file=str(file_path))
            if downloaded_path:
                # Проверяем размер
                size = Path(downloaded_path).stat().st_size
                # Определяем mime_type
                mime_type = None
                if hasattr(media, 'document'):
                    mime_type = getattr(media.document, 'mime_type', None)
                elif hasattr(media, 'photo'):
                    mime_type = 'image/jpeg'
                telegram_file_id = None
                if hasattr(media, 'document'):
                    telegram_file_id = media.document.id
                elif hasattr(media, 'photo'):
                    telegram_file_id = media.photo.id

                relative_path = str(Path(downloaded_path).relative_to(BASE_DIR))
                logger.info(f"Скачан файл: {relative_path} ({size} байт)")
                return [{
                    'file_path': relative_path,
                    'file_name': file_name,
                    'file_size': size,
                    'mime_type': mime_type,
                    'telegram_file_id': str(telegram_file_id) if telegram_file_id else None
                }]
            else:
                logger.warning(f"Не удалось скачать медиа для сообщения {message_id}")
                return None
        except Exception as e:
            logger.error(f"Ошибка при скачивании медиа для сообщения {message_id}: {e}")
            return None
    else:
        # Неподдерживаемый тип медиа
        return None


# ----------------------------------------------------------------------
# Основная функция загрузки чата
# ----------------------------------------------------------------------
async def load_chat(chat_reference, client: TelegramClient, db: Database):
    """
    Загрузить все сообщения из указанного чата.
    """
    logger.info(f"Начало загрузки чата: {chat_reference}")

    try:
        entity = await client.get_entity(chat_reference)
    except Exception as e:
        logger.error(f"Не удалось получить сущность чата {chat_reference}: {e}")
        return

    # Определяем параметры чата
    chat_telegram_id = entity.id
    if isinstance(entity, PeerChannel):
        chat_telegram_id = entity.channel_id
    elif isinstance(entity, PeerChat):
        chat_telegram_id = entity.chat_id
    elif isinstance(entity, PeerUser):
        chat_telegram_id = entity.user_id

    # Если это отрицательный ID для супергрупп, обычно он уже отрицательный
    # Приводим к int
    chat_telegram_id = int(chat_telegram_id)

    title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(chat_telegram_id)
    chat_type = get_chat_type(entity)
    username = getattr(entity, 'username', None)

    # Проверяем, есть ли чат в БД, или вставляем
    chat_record = await db.get_chat_by_telegram_id(chat_telegram_id)
    if not chat_record:
        chat_internal_id = await db.insert_chat(chat_telegram_id, title, chat_type, username)
        logger.info(f"Добавлен новый чат: {title} (ID {chat_telegram_id})")
    else:
        chat_internal_id = chat_record['id']
        # Если поменялось название, можно обновить, но не обязательно
        logger.info(f"Используем существующий чат: {title} (ID {chat_telegram_id})")

    # Получаем последний загруженный ID
    last_loaded_id = await db.get_last_loaded_id(chat_internal_id)
    logger.info(f"Последний загруженный message_id: {last_loaded_id}")

    # Цикл загрузки сообщений от старых к новым
    total_loaded = 0
    while True:
        try:
            # Запрашиваем сообщения с ID > last_loaded_id, по возрастанию
            messages = await client.get_messages(
                entity,
                min_id=last_loaded_id + 1,
                reverse=True,
                limit=BATCH_SIZE
            )
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"Превышение лимита запросов. Ожидание {wait_time} секунд.")
            await asyncio.sleep(wait_time)
            continue
        except RPCError as e:
            logger.error(f"RPC ошибка при получении сообщений: {e}")
            break

        if not messages:
            logger.info("Новых сообщений нет.")
            break

        logger.info(f"Получено {len(messages)} сообщений (от ID {messages[0].id} до {messages[-1].id})")

        # Обрабатываем каждое сообщение
        for msg in messages:
            # Проверка дубликата
            exists = await db.message_exists(chat_internal_id, msg.id)
            if exists:
                logger.debug(f"Сообщение {msg.id} уже есть в БД, пропускаем.")
                continue

            # Извлекаем информацию об авторе
            author_id, author_name = extract_author_info(msg)

            # Текст
            text = msg.text if hasattr(msg, 'text') else None

            # Дата
            date = msg.date

            # Ответ на сообщение
            reply_to = None
            if hasattr(msg, 'reply_to') and msg.reply_to:
                reply_to = msg.reply_to.reply_to_msg_id

            # ID темы (если есть)
            thread_id = None
            if hasattr(msg, 'reply_to_top_id'):
                thread_id = msg.reply_to_top_id
            elif hasattr(msg, 'thread_id'):
                thread_id = msg.thread_id

            # Реакции
            reactions_json = extract_reactions(msg)

            # Сырые данные (опционально)
            raw_data = None

            # Вставляем сообщение
            msg_db_id = await db.insert_message(
                chat_internal_id,
                msg.id,
                thread_id,
                author_id,
                author_name,
                date,
                text,
                reply_to,
                reactions_json,
                raw_data
            )

            if not msg_db_id:
                # Если не удалось вставить, возможно дубликат, пропускаем
                continue

            # Скачиваем медиа, если есть
            if msg.media:
                attachments_info = await download_media(msg, chat_telegram_id, msg.id)
                if attachments_info:
                    for att in attachments_info:
                        await db.insert_attachment(
                            msg_db_id,
                            att['file_path'],
                            att['file_name'],
                            att['file_size'],
                            att['mime_type'],
                            att['telegram_file_id']
                        )

            # Обновляем last_loaded_id
            if msg.id > last_loaded_id:
                last_loaded_id = msg.id

            total_loaded += 1

        # После обработки пачки обновляем last_loaded_id в БД
        await db.update_last_loaded_id(chat_internal_id, last_loaded_id)
        logger.info(f"Обновлён last_loaded_id = {last_loaded_id} для чата {title}")

        # Если количество сообщений меньше BATCH_SIZE, значит это последняя партия
        if len(messages) < BATCH_SIZE:
            break

        # Небольшая задержка, чтобы не флудить
        await asyncio.sleep(1)

    logger.info(f"Загрузка чата {title} завершена. Всего загружено {total_loaded} новых сообщений.")


# ----------------------------------------------------------------------
# Основная функция
# ----------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description='Telegram History Exporter')
    parser.add_argument('--chat', action='append', help='Загрузить только указанный чат (можно указать несколько)')
    args = parser.parse_args()

    # Определяем список чатов для загрузки
    if args.chat:
        chats_to_load = args.chat
        logger.info(f"Загрузка указанных чатов: {chats_to_load}")
    else:
        chats_to_load = CHATS
        logger.info(f"Загрузка чатов из config.py: {chats_to_load}")

    if not chats_to_load:
        logger.warning("Список чатов пуст. Завершение.")
        return

    # Инициализация БД
    db = Database(DB_PATH)
    await db.init()
    logger.info("База данных инициализирована.")

    # Создаем клиент Telegram
    client = TelegramClient('session', API_ID, API_HASH)
    await client.start()

    try:
        for chat_ref in chats_to_load:
            await load_chat(chat_ref, client, db)
    finally:
        await client.disconnect()
        logger.info("Клиент отключён.")


if __name__ == '__main__':
    asyncio.run(main())