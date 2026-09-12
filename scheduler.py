#!/usr/bin/env python3
"""
Планировщик синхронизации Telegram-каналов по cron из config.py.
"""
import asyncio
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from exporter import (
    authorize_client,
    create_client,
    load_api_credentials,
    load_settings,
    run_export,
    setup_logging,
)


async def ensure_session() -> None:
    api_id, api_hash = load_api_credentials()
    client = create_client(api_id, api_hash)
    try:
        await authorize_client(client, api_id, api_hash, interactive=False)
    finally:
        if client.is_connected():
            await client.disconnect()


async def main() -> int:
    logger = setup_logging('TelegramScheduler')
    settings = load_settings()

    if not settings.schedule_cron:
        logger.error('SCHEDULE_CRON не задан в config.py. Завершение.')
        return 1

    try:
        trigger = CronTrigger.from_crontab(
            settings.schedule_cron,
            timezone=settings.schedule_timezone,
        )
    except Exception as e:
        logger.error(f'Некорректный SCHEDULE_CRON ({settings.schedule_cron}): {e}')
        return 1

    try:
        await ensure_session()
    except Exception as e:
        logger.error(f'Ошибка авторизации: {e}')
        return 1

    async def sync_job():
        logger.info('Запуск синхронизации по расписанию')
        try:
            await run_export(
                settings.chats,
                download_attachments=settings.download_attachments,
                max_file_size_bytes=settings.max_file_size_bytes,
                interactive=False,
            )
        except Exception as e:
            logger.error(f'Синхронизация завершилась с ошибкой: {e}')
        job = scheduler.get_job('tg_sync')
        if job and job.next_run_time:
            logger.info(f'Следующий запуск: {job.next_run_time}')

    scheduler = AsyncIOScheduler(timezone=settings.schedule_timezone)
    scheduler.add_job(
        sync_job,
        trigger,
        id='tg_sync',
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()

    tz_label = settings.schedule_timezone or 'local'
    logger.info(f'Планировщик запущен. Cron: {settings.schedule_cron} ({tz_label})')
    job = scheduler.get_job('tg_sync')
    if job and job.next_run_time:
        logger.info(f'Следующий запуск: {job.next_run_time}')

    if settings.schedule_run_on_start:
        logger.info('SCHEDULE_RUN_ON_START: выполняем синхронизацию сразу.')
        await sync_job()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    logger.info('Ожидание расписания. Остановка: Ctrl+C.')
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    scheduler.shutdown(wait=False)
    logger.info('Планировщик остановлен.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
