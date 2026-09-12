#!/usr/bin/env python3
"""
Докачка отсутствующих вложений из таблицы attachments.

Ищет записи, для которых файла нет на диске, и скачивает их из Telegram.
"""
import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from typing import Optional

from telethon.errors import FloodWaitError, RPCError

from exporter import (
    DB_PATH,
    Database,
    authorize_client,
    create_client,
    ensure_dirs,
    get_media_size,
    human_size,
    load_api_credentials,
    load_settings,
    mb_to_bytes,
    resolve_attachment_path,
    setup_logging,
    should_skip_by_size,
)

logger = logging.getLogger('TelegramDownloader')


def chat_matches(row, chat_ref: str) -> bool:
    ref = chat_ref.strip().lstrip('@')
    if ref.startswith('https://'):
        ref = ref[len('https://'):]
    if ref.startswith('http://'):
        ref = ref[len('http://'):]
    ref_l = ref.lower()

    telegram_id = str(row['chat_telegram_id'])
    if ref == telegram_id or ref.lstrip('-') == telegram_id.lstrip('-'):
        return True

    username = (row['username'] or '').lower()
    if username and (
        username == ref_l
        or ref_l.endswith(f't.me/{username}')
        or ref_l == f't.me/{username}'
    ):
        return True

    title = (row['title'] or '').lower()
    return bool(title) and title == ref_l


async def resolve_entity(client, telegram_id, username):
    try:
        return await client.get_entity(telegram_id)
    except Exception:
        if username:
            return await client.get_entity(username)
        raise


async def fetch_message(client, entity, message_id):
    try:
        return await client.get_messages(entity, ids=message_id)
    except FloodWaitError as e:
        logger.warning(f'Превышение лимита запросов. Ожидание {e.seconds} секунд.')
        await asyncio.sleep(e.seconds)
        return await client.get_messages(entity, ids=message_id)


async def download_to_path(message, target):
    try:
        return await message.download_media(file=str(target))
    except FloodWaitError as e:
        logger.warning(f'Превышение лимита запросов. Ожидание {e.seconds} секунд.')
        await asyncio.sleep(e.seconds)
        return await message.download_media(file=str(target))


async def download_missing(
    chats: Optional[list],
    max_file_size_bytes: Optional[int],
    force: bool,
) -> None:
    ensure_dirs()
    if not DB_PATH.exists():
        logger.error(f'База данных не найдена: {DB_PATH}')
        return

    db = Database(DB_PATH)
    await db.init()
    rows = await db.list_attachments_with_chats()
    if chats:
        rows = [row for row in rows if any(chat_matches(row, ref) for ref in chats)]

    missing = []
    for row in rows:
        path = resolve_attachment_path(row['file_path'])
        if not path.exists() or path.stat().st_size == 0:
            missing.append(row)

    if not missing:
        logger.info('Отсутствующих файлов нет.')
        return

    logger.info(f'К докачке: {len(missing)} файл(ов).')

    api_id, api_hash = load_api_credentials()
    client = create_client(api_id, api_hash)
    try:
        await authorize_client(client, api_id, api_hash, interactive=True)
    except Exception as e:
        logger.error(f'Ошибка авторизации: {e}')
        if client.is_connected():
            await client.disconnect()
        raise

    by_chat = defaultdict(list)
    for row in missing:
        by_chat[row['chat_telegram_id']].append(row)

    downloaded = 0
    skipped = 0
    failed = 0

    try:
        for chat_telegram_id, items in by_chat.items():
            username = items[0]['username']
            title = items[0]['title'] or username or chat_telegram_id
            try:
                entity = await resolve_entity(client, chat_telegram_id, username)
            except Exception as e:
                logger.error(f'Не удалось получить чат {title}: {e}')
                failed += len(items)
                continue

            logger.info(f'Докачка {len(items)} файл(ов) из чата {title}')
            for row in items:
                target = resolve_attachment_path(row['file_path'])
                size = row['file_size']
                if not force and should_skip_by_size(size, max_file_size_bytes):
                    logger.info(
                        f'Пропуск {row["file_name"]}: {human_size(size)} > лимит '
                        f'{human_size(max_file_size_bytes)}'
                    )
                    skipped += 1
                    continue

                try:
                    msg = await fetch_message(client, entity, row['tg_message_id'])
                except RPCError as e:
                    logger.error(f'RPC ошибка для сообщения {row["tg_message_id"]}: {e}')
                    failed += 1
                    continue
                except Exception as e:
                    logger.error(f'Не удалось получить сообщение {row["tg_message_id"]}: {e}')
                    failed += 1
                    continue

                if msg is None:
                    logger.warning(f'Сообщение {row["tg_message_id"]} в чате {title} не найдено.')
                    failed += 1
                    continue

                media_size = get_media_size(msg.media) if getattr(msg, 'media', None) else None
                check_size = size if size else media_size
                if not force and should_skip_by_size(check_size, max_file_size_bytes):
                    logger.info(
                        f'Пропуск {row["file_name"]}: {human_size(check_size)} > лимит '
                        f'{human_size(max_file_size_bytes)}'
                    )
                    skipped += 1
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    downloaded_path = await download_to_path(msg, target)
                except Exception as e:
                    logger.error(f'Ошибка скачивания {row["file_name"]}: {e}')
                    failed += 1
                    continue

                if not downloaded_path:
                    logger.warning(
                        f'Не удалось скачать {row["file_name"]} (сообщение {row["tg_message_id"]})'
                    )
                    failed += 1
                    continue

                actual_size = target.stat().st_size if target.exists() else 0
                if actual_size:
                    await db.update_attachment_size(row['id'], actual_size)
                logger.info(f'Скачан файл: {row["file_path"]} ({actual_size} байт)')
                downloaded += 1
    finally:
        await client.disconnect()
        logger.info('Клиент отключён.')

    logger.info(
        f'Докачка завершена. Скачано: {downloaded}, пропущено по размеру: {skipped}, ошибок: {failed}.'
    )


async def async_main():
    settings = load_settings()
    parser = argparse.ArgumentParser(
        description='Докачка отсутствующих файлов из таблицы attachments'
    )
    parser.add_argument(
        '--chat',
        action='append',
        help='Только указанный чат (можно указать несколько)',
    )
    parser.add_argument(
        '--max-file-size',
        type=float,
        metavar='MB',
        help='Не скачивать файлы больше указанного размера (в мегабайтах)',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Игнорировать лимит размера файла',
    )
    args = parser.parse_args()

    setup_logging('TelegramDownloader')

    max_file_size_mb = None if args.force else (
        settings.max_file_size_mb if args.max_file_size is None else args.max_file_size
    )
    max_file_size_bytes = mb_to_bytes(max_file_size_mb)

    if args.force:
        logger.info('Режим --force: лимит размера файла игнорируется.')
    elif max_file_size_bytes is not None:
        logger.info(f'Лимит размера файла: {max_file_size_mb} МБ.')

    await download_missing(args.chat, max_file_size_bytes, force=args.force)


if __name__ == '__main__':
    try:
        asyncio.run(async_main())
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
