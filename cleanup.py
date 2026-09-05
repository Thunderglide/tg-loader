#!/usr/bin/env python3
"""
Очистка локальной базы SQLite и скачанных вложений.

Удаляет data/telegram_export.db (и sidecar-файлы SQLite) и содержимое data/files/.
Сессия Telethon (session.session) и логи не трогаются.
"""
import argparse
import shutil
import sys
from pathlib import Path

BASE_DIR = Path('data')
DB_PATH = BASE_DIR / 'telegram_export.db'
FILES_DIR = BASE_DIR / 'files'

DB_SIDECARS = (
    DB_PATH,
    Path(str(DB_PATH) + '-journal'),
    Path(str(DB_PATH) + '-wal'),
    Path(str(DB_PATH) + '-shm'),
)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{num_bytes} B'


def collect_targets() -> tuple[list[Path], int, int]:
    """Возвращает (файлы/папки к удалению, число файлов, суммарный размер)."""
    targets: list[Path] = []
    file_count = 0
    total_size = 0

    for path in DB_SIDECARS:
        if path.exists():
            targets.append(path)
            file_count += 1
            total_size += path.stat().st_size

    if FILES_DIR.exists():
        nested_files = [p for p in FILES_DIR.rglob('*') if p.is_file()]
        if nested_files or any(FILES_DIR.iterdir()):
            targets.append(FILES_DIR)
            file_count += len(nested_files)
            total_size += sum(p.stat().st_size for p in nested_files)

    return targets, file_count, total_size


def confirm() -> bool:
    answer = input('Удалить базу и скачанные файлы? [y/N]: ').strip().lower()
    return answer in ('y', 'yes', 'д', 'да')


def cleanup(targets: list[Path]) -> None:
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
            print(f'Удалена папка: {path}')
        elif path.exists():
            path.unlink()
            print(f'Удалён файл: {path}')

    FILES_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Очистка базы данных и скачанных файлов Telegram-экспорта'
    )
    parser.add_argument(
        '-y', '--yes',
        action='store_true',
        help='Не спрашивать подтверждение',
    )
    args = parser.parse_args()

    targets, file_count, total_size = collect_targets()
    if not targets:
        print('Нечего удалять: база и файлы уже отсутствуют.')
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        return 0

    print(f'Будет удалено: {file_count} файл(ов), {human_size(total_size)}')
    for path in targets:
        print(f'  - {path}')

    if not args.yes and not confirm():
        print('Отменено.')
        return 1

    cleanup(targets)
    print('Очистка завершена.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
