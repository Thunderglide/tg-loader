#!/usr/bin/env python3
"""
Telegram History Exporter
Скрипт для выгрузки истории сообщений из Telegram (каналы, группы, суперчаты с темами)
с сохранением сообщений и вложений в локальную SQLite базу и на диск.
"""
import argparse
import asyncio
import sys

from exporter import load_settings, mb_to_bytes, run_export, setup_logging


async def main():
    settings = load_settings()
    parser = argparse.ArgumentParser(description='Telegram History Exporter')
    parser.add_argument(
        '--chat',
        action='append',
        help='Загрузить только указанный чат (можно указать несколько)',
    )
    parser.add_argument(
        '--no-attachments',
        action='store_true',
        help='Сохранять сообщения без скачивания файлов',
    )
    parser.add_argument(
        '--max-file-size',
        type=float,
        metavar='MB',
        help='Не скачивать файлы больше указанного размера (в мегабайтах)',
    )
    args = parser.parse_args()

    logger = setup_logging('TelegramExporter')

    if args.chat:
        chats_to_load = args.chat
        logger.info(f'Загрузка указанных чатов: {chats_to_load}')
    else:
        chats_to_load = settings.chats
        logger.info(f'Загрузка чатов из config.py: {chats_to_load}')

    download_attachments = False if args.no_attachments else settings.download_attachments
    max_file_size_mb = settings.max_file_size_mb if args.max_file_size is None else args.max_file_size
    max_file_size_bytes = mb_to_bytes(max_file_size_mb)

    if not download_attachments:
        logger.info('Режим без вложений: файлы на диск не скачиваются.')
    elif max_file_size_bytes is not None:
        logger.info(f'Лимит размера файла: {max_file_size_mb} МБ.')

    await run_export(
        chats_to_load,
        download_attachments=download_attachments,
        max_file_size_bytes=max_file_size_bytes,
        interactive=True,
    )


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
