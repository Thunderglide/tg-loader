#!/usr/bin/env python3
"""
Telegram History Exporter
Скрипт для выгрузки истории сообщений из Telegram (каналы, группы, суперчаты с темами)
с сохранением сообщений и вложений в локальную SQLite базу и на диск.
"""
import os
import sys
import json
import base64
import getpass
import argparse
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

from dotenv import load_dotenv
from telethon import TelegramClient, utils
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.tlobject import TLObject
from telethon.tl.types import (
    Message, MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage, MessageMediaGame, MessageMediaInvoice,
    MessageMediaGeo, MessageMediaContact, MessageMediaDice,
    PeerChannel, PeerChat, PeerUser, CodeSettings
)
from telethon.errors import (
    RPCError, FloodWaitError, AuthRestartError,
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PhoneCodeEmptyError,
    PhoneNumberInvalidError, PhoneNumberBannedError,
)
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
            await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id)')
            await db.commit()

    async def get_chat_by_telegram_id(self, telegram_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT id, telegram_id, title, type, username, last_loaded_id FROM chats WHERE telegram_id = ?',
                (telegram_id,)
            ) as cursor:
                return await cursor.fetchone()

    async def insert_chat(self, telegram_id: int, title: str, chat_type: str, username: str = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR IGNORE INTO chats (telegram_id, title, type, username) VALUES (?, ?, ?, ?)',
                (telegram_id, title, chat_type, username)
            )
            await db.commit()
            async with db.execute('SELECT id FROM chats WHERE telegram_id = ?', (telegram_id,)) as c:
                row = await c.fetchone()
                return row[0] if row else None

    async def update_last_loaded_id(self, chat_id: int, last_loaded_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE chats SET last_loaded_id = ? WHERE id = ?', (last_loaded_id, chat_id))
            await db.commit()

    async def get_last_loaded_id(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT last_loaded_id FROM chats WHERE id = ?', (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def message_exists(self, chat_id: int, message_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?', (chat_id, message_id)) as cursor:
                return await cursor.fetchone() is not None

    async def insert_message(self, chat_id: int, message_id: int, thread_id: Optional[int],
                             author_id: Optional[int], author_name: Optional[str],
                             date: datetime, text: Optional[str], reply_to_msg_id: Optional[int],
                             reactions_json: Optional[str], raw_data: Optional[str] = None):
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
                async with db.execute('SELECT id FROM messages WHERE chat_id = ? AND message_id = ?', (chat_id, message_id)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
            except Exception as e:
                logger.error(f"Ошибка вставки сообщения (chat_id={chat_id}, msg_id={message_id}): {e}")
                return None

    async def insert_attachment(self, message_db_id: int, file_path: str, file_name: str,
                                file_size: int, mime_type: str, telegram_file_id: str):
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


# Поля TL-сообщения, которые сохраняем в raw_data (без служебных атрибутов Telethon).
RAW_MESSAGE_FIELDS = (
    'id', 'peer_id', 'date', 'message', 'out', 'mentioned', 'media_unread',
    'silent', 'post', 'from_scheduled', 'legacy', 'edit_hide', 'pinned',
    'noforwards', 'invert_media', 'offline', 'video_processing_pending',
    'paid_suggested_post_stars', 'paid_suggested_post_ton', 'from_id',
    'from_boosts_applied', 'from_rank', 'saved_peer_id', 'fwd_from',
    'via_bot_id', 'via_business_bot_id', 'guestchat_via_from', 'reply_to',
    'media', 'reply_markup', 'entities', 'views', 'forwards', 'replies',
    'edit_date', 'post_author', 'grouped_id', 'reactions', 'restriction_reason',
    'ttl_period', 'quick_reply_shortcut_id', 'effect', 'factcheck',
    'report_delivery_until_date', 'paid_message_stars', 'suggested_post',
    'schedule_repeat_period', 'summary_from_language', 'rich_message',
    'action', 'reactions_are_possible',
)


def _json_default(value):
    """Сериализация datetime/bytes/вложенных TL-объектов в JSON."""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode('ascii')
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, TLObject):
        try:
            return value.to_dict()
        except NotImplementedError:
            return str(value)
    return str(value)


def serialize_raw_data(message: Message) -> Optional[str]:
    """Полный JSON исходного сообщения Telegram для колонки raw_data."""
    try:
        payload = {
            '_': 'MessageService' if getattr(message, 'action', None) else 'Message'
        }
        for field in RAW_MESSAGE_FIELDS:
            value = getattr(message, field, None)
            if value is None:
                continue
            if isinstance(value, TLObject):
                try:
                    payload[field] = value.to_dict()
                except NotImplementedError:
                    payload[field] = str(value)
            elif isinstance(value, list):
                items = []
                for item in value:
                    if isinstance(item, TLObject):
                        try:
                            items.append(item.to_dict())
                        except NotImplementedError:
                            items.append(str(item))
                    else:
                        items.append(item)
                payload[field] = items
            else:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.warning(
            f"Не удалось сериализовать raw_data для сообщения {getattr(message, 'id', '?')}: {e}"
        )
        return None


def append_file_links(text: Optional[str], attachments: Optional[List[Dict]]) -> Optional[str]:
    """Добавляет в текст относительные ссылки на файлы, приложенные к сообщению."""
    if not attachments:
        return text

    links = []
    for att in attachments:
        rel = att.get('file_path')
        if not rel:
            continue
        links.append(Path(rel).as_posix())

    if not links:
        return text

    extra = '\n'.join(links)
    if text and text.strip():
        return f'{text.rstrip()}\n\n{extra}'
    return extra


def _reaction_info(reaction) -> dict:
    """Тип и значение эмодзи из TL-объекта Reaction*."""
    if reaction is None:
        return {}
    if hasattr(reaction, 'emoticon') and reaction.emoticon:
        return {'type': 'emoji', 'emoticon': reaction.emoticon}
    if hasattr(reaction, 'document_id') and reaction.document_id:
        return {'type': 'custom_emoji', 'custom_emoji_id': str(reaction.document_id)}
    type_name = type(reaction).__name__
    if type_name == 'ReactionPaid':
        return {'type': 'paid'}
    info = {'type': type_name}
    if hasattr(reaction, 'to_dict'):
        try:
            extra = reaction.to_dict()
            extra.pop('_', None)
            info.update(extra)
        except Exception:
            pass
    return info


def extract_reactions(message: Message) -> Optional[str]:
    if not message.reactions:
        return None
    reactions_list = []
    results = getattr(message.reactions, 'results', None) or []
    for r in results:
        count = getattr(r, 'count', 0)
        if not count:
            continue
        item = {'count': count}
        item.update(_reaction_info(getattr(r, 'reaction', None) or r))
        reactions_list.append(item)
    return json.dumps(reactions_list, ensure_ascii=False) if reactions_list else None


async def download_media(message: Message, chat_telegram_id: int, message_id: int) -> Optional[List[Dict]]:
    if not message.media:
        return None

    media = message.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument,
                          MessageMediaWebPage, MessageMediaGame,
                          MessageMediaInvoice, MessageMediaGeo,
                          MessageMediaContact, MessageMediaDice)):
        dir_path = FILES_DIR / str(chat_telegram_id) / str(message_id)
        dir_path.mkdir(parents=True, exist_ok=True)

        file_name = None
        if hasattr(media, 'document') and media.document:
            for attr in media.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    file_name = attr.file_name
                    break
        elif hasattr(media, 'photo') and media.photo:
            file_name = f"photo_{message_id}.jpg"
        elif hasattr(media, 'webpage') and media.webpage:
            return None
        else:
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
                'file_path': file_path.relative_to(BASE_DIR).as_posix(),
                'file_name': file_name,
                'file_size': file_size,
                'mime_type': mime_type,
                'telegram_file_id': str(telegram_file_id) if telegram_file_id else None
            }]

        try:
            downloaded_path = await message.download_media(file=str(file_path))
            if downloaded_path:
                size = Path(downloaded_path).stat().st_size
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

                relative_path = Path(downloaded_path).relative_to(BASE_DIR).as_posix()
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
        return None


# ----------------------------------------------------------------------
# Загрузка чата
# ----------------------------------------------------------------------
async def load_chat(chat_reference, client: TelegramClient, db: Database):
    logger.info(f"Начало загрузки чата: {chat_reference}")

    try:
        entity = await client.get_entity(chat_reference)
    except Exception as e:
        logger.error(f"Не удалось получить сущность чата {chat_reference}: {e}")
        return

    chat_telegram_id = entity.id
    if isinstance(entity, PeerChannel):
        chat_telegram_id = entity.channel_id
    elif isinstance(entity, PeerChat):
        chat_telegram_id = entity.chat_id
    elif isinstance(entity, PeerUser):
        chat_telegram_id = entity.user_id

    chat_telegram_id = int(chat_telegram_id)
    title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(chat_telegram_id)
    chat_type = get_chat_type(entity)
    username = getattr(entity, 'username', None)

    chat_record = await db.get_chat_by_telegram_id(chat_telegram_id)
    if not chat_record:
        chat_internal_id = await db.insert_chat(chat_telegram_id, title, chat_type, username)
        logger.info(f"Добавлен новый чат: {title} (ID {chat_telegram_id})")
    else:
        chat_internal_id = chat_record['id']
        logger.info(f"Используем существующий чат: {title} (ID {chat_telegram_id})")

    last_loaded_id = await db.get_last_loaded_id(chat_internal_id)
    logger.info(f"Последний загруженный message_id: {last_loaded_id}")

    total_loaded = 0
    while True:
        try:
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

        for msg in messages:
            exists = await db.message_exists(chat_internal_id, msg.id)
            if exists:
                logger.debug(f"Сообщение {msg.id} уже есть в БД, пропускаем.")
                continue

            author_id, author_name = extract_author_info(msg)
            text = msg.text if hasattr(msg, 'text') else None
            date = msg.date

            reply_to = None
            if hasattr(msg, 'reply_to') and msg.reply_to:
                reply_to = msg.reply_to.reply_to_msg_id

            thread_id = None
            if hasattr(msg, 'reply_to_top_id'):
                thread_id = msg.reply_to_top_id
            elif hasattr(msg, 'thread_id'):
                thread_id = msg.thread_id

            reactions_json = extract_reactions(msg)
            raw_data = serialize_raw_data(msg)

            attachments_info = None
            if msg.media:
                attachments_info = await download_media(msg, chat_telegram_id, msg.id)
                text = append_file_links(text, attachments_info)

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
                continue

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

            if msg.id > last_loaded_id:
                last_loaded_id = msg.id

            total_loaded += 1

        await db.update_last_loaded_id(chat_internal_id, last_loaded_id)
        logger.info(f"Обновлён last_loaded_id = {last_loaded_id} для чата {title}")

        if len(messages) < BATCH_SIZE:
            break

        await asyncio.sleep(1)

    logger.info(f"Загрузка чата {title} завершена. Всего загружено {total_loaded} новых сообщений.")


# ----------------------------------------------------------------------
# Авторизация (Telethon 1.44: номер телефона + код из приложения)
# ----------------------------------------------------------------------
def describe_sent_code(sent) -> str:
    """Подсказка, куда Telegram отправил код."""
    type_name = type(sent.type).__name__
    length = getattr(sent.type, 'length', None)
    hints = {
        'SentCodeTypeApp': (
            'Код отправлен в официальное приложение Telegram. '
            'Откройте Telegram на телефоне: уведомление или чат «Telegram» (не SMS).'
        ),
        'SentCodeTypeSms': 'Код должен прийти по SMS.',
        'SentCodeTypeCall': 'Ожидается звонок с кодом.',
        'SentCodeTypeFlashCall': 'Ожидается flash-call.',
        'SentCodeTypeMissedCall': 'Ожидается пропущенный звонок.',
        'SentCodeTypeEmailCode': (
            f"Код отправлен на email {getattr(sent.type, 'email_pattern', '')}."
        ),
        'SentCodeTypeSetUpEmailRequired': (
            'Telegram требует привязать email. Сначала войдите в официальном приложении.'
        ),
        'SentCodeTypeFirebaseSms': (
            'Telegram пытается отправить код через Firebase SMS — '
            'в Python-клиенте он часто не доходит. Смотрите чат «Telegram» в приложении.'
        ),
        'SentCodeTypeFragmentSms': 'Код через Fragment SMS.',
        'SentCodeTypeSmsWord': 'Код — слово из SMS.',
        'SentCodeTypeSmsPhrase': 'Код — фраза из SMS.',
    }
    msg = hints.get(type_name, f'Код отправлен ({type_name}).')
    if length:
        msg += f' Длина: {length} символов.'
    return msg


async def prompt_async(prompt: str, *, secret: bool = False) -> str:
    """Ввод в терминале без блокировки event loop."""
    if secret:
        value = await asyncio.to_thread(getpass.getpass, prompt)
    else:
        value = await asyncio.to_thread(input, prompt)
    return value.strip()


async def request_login_code(client: TelegramClient, phone: str):
    """Запросить код входа. allow_app_hash=True — доставка в официальное приложение."""
    for _ in range(3):
        try:
            return await client(SendCodeRequest(
                phone_number=phone,
                api_id=API_ID,
                api_hash=API_HASH,
                settings=CodeSettings(allow_app_hash=True),
            ))
        except AuthRestartError:
            logger.warning('Telegram попросил перезапустить авторизацию, повторяем запрос кода.')
        except FloodWaitError as e:
            logger.warning(f'Лимит запросов кода. Ожидание {e.seconds} сек.')
            await asyncio.sleep(e.seconds)
    raise RuntimeError('Не удалось запросить код подтверждения у Telegram.')


async def authorize_client(client: TelegramClient) -> None:
    """
    Подключение и вход по коду из Telegram.

    Код приходит в официальное приложение, не по SMS.
    Номер можно задать в .env как PHONE=+79001234567.
    Если сессия уже есть, повторный ввод не нужен.
    """
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        display_name = getattr(me, 'username', None) or getattr(me, 'first_name', None) or 'unknown'
        logger.info(f'Уже авторизованы: {display_name} (id={me.id})')
        return

    phone = os.getenv('PHONE', '').strip()
    if not phone:
        phone = await prompt_async(
            'Введите номер телефона (с кодом страны, например +79001234567): '
        )

    phone = utils.parse_phone(phone)
    if not phone:
        raise ValueError('Некорректный номер телефона. Нужен формат +79001234567.')

    logger.info(f'Запрашиваем код подтверждения для +{phone}...')
    sent = await request_login_code(client, phone)
    logger.info(describe_sent_code(sent))
    print(describe_sent_code(sent))

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        code = await prompt_async('Введите код подтверждения из Telegram: ')
        if not code:
            logger.warning('Пустой код, запросите новый, если прежний уже недействителен.')
            continue
        try:
            await client.sign_in(phone, code=code, phone_code_hash=sent.phone_code_hash)
            break
        except PhoneCodeExpiredError:
            logger.warning('Код истёк. Запрашиваем новый...')
            sent = await request_login_code(client, phone)
            logger.info(describe_sent_code(sent))
            print(describe_sent_code(sent))
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            left = max_attempts - attempt
            logger.warning(
                f'Неверный код. Осталось попыток: {left}. '
                'Не пересылайте код из чата «Telegram» — от этого он сразу сгорает.'
            )
            if left <= 0:
                raise
        except SessionPasswordNeededError:
            password = await prompt_async(
                'Введите пароль двухфакторной аутентификации: ',
                secret=True,
            )
            await client.sign_in(password=password)
            break
        except (PhoneNumberInvalidError, PhoneNumberBannedError):
            raise
    else:
        raise RuntimeError('Не удалось войти: код так и не принят.')

    me = await client.get_me()
    display_name = getattr(me, 'username', None) or getattr(me, 'first_name', None) or 'unknown'
    logger.info(f'Авторизация успешна: {display_name} (id={me.id})')


# ----------------------------------------------------------------------
# Основная функция
# ----------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description='Telegram History Exporter')
    parser.add_argument('--chat', action='append', help='Загрузить только указанный чат (можно указать несколько)')
    args = parser.parse_args()

    if args.chat:
        chats_to_load = args.chat
        logger.info(f"Загрузка указанных чатов: {chats_to_load}")
    else:
        chats_to_load = CHATS
        logger.info(f"Загрузка чатов из config.py: {chats_to_load}")

    if not chats_to_load:
        logger.warning("Список чатов пуст. Завершение.")
        return

    db = Database(DB_PATH)
    await db.init()
    logger.info("База данных инициализирована.")

    # Telethon 1.44 по умолчанию шлёт system_lang_code='en' — из-за этого
    # Telegram часто не доставляет код в приложение. Нужен en-US.
    client = TelegramClient(
        'session',
        API_ID,
        API_HASH,
        device_model='MacBook Pro',
        system_version='macOS 15.5',
        app_version='1.44.0',
        lang_code='en',
        system_lang_code='en-US',
    )
    try:
        await authorize_client(client)
    except Exception as e:
        logger.error(f"Ошибка авторизации: {e}")
        if client.is_connected():
            await client.disconnect()
        return

    try:
        for chat_ref in chats_to_load:
            await load_chat(chat_ref, client, db)
    finally:
        await client.disconnect()
        logger.info("Клиент отключён.")


if __name__ == '__main__':
    asyncio.run(main())