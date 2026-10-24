"""NovelCraft AI — 数据库备份/恢复工具

用法:
  python scripts/backup_db.py                    # 备份到 test_artifacts/
  python scripts/backup_db.py --restore <file>   # 从指定文件恢复
  python scripts/backup_db.py --list             # 列出已有备份
"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'backend', 'data', 'novelcraft.db')
BACKUP_DIR = os.path.join(os.path.dirname(__file__), '..', 'test_artifacts', 'db_backups')


def _ensure_dir() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)


def backup() -> str:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f'数据库文件不存在: {DB_PATH}')

    _ensure_dir()
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    size_bytes = os.path.getsize(DB_PATH)
    name = f'novelcraft-{timestamp}-{size_bytes}b.db'
    dest = os.path.join(BACKUP_DIR, name)
    shutil.copy2(DB_PATH, dest)
    print(f'✓ 备份完成: {dest}')
    print(f'  大小: {size_bytes:,} 字节')
    return dest


def restore(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f'备份文件不存在: {path}')

    # 先做一份当前库的安全备份
    if os.path.exists(DB_PATH):
        safety = DB_PATH + f'.pre_restore_{datetime.now().strftime("%Y%m%d-%H%M%S")}'
        shutil.copy2(DB_PATH, safety)
        print(f'✓ 恢复前已安全备份到: {safety}')

    shutil.copy2(path, DB_PATH)
    print(f'✓ 已从 {path} 恢复到 {DB_PATH}')


def list_backups() -> list[str]:
    if not os.path.exists(BACKUP_DIR):
        print('(暂无备份)')
        return []
    files = sorted(os.listdir(BACKUP_DIR), reverse=True)
    for i, f in enumerate(files):
        full = os.path.join(BACKUP_DIR, f)
        size = os.path.getsize(full)
        print(f'  {i+1:3d}. {f}  ({size:,} 字节)')
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description='NovelCraft AI 数据库备份/恢复')
    parser.add_argument('--restore', metavar='FILE', help='从指定备份文件恢复')
    parser.add_argument('--list', action='store_true', help='列出已有备份')
    args = parser.parse_args()

    if args.list:
        list_backups()
    elif args.restore:
        confirm = input(f'⚠️ 将用 {args.restore} 覆盖当前数据库，确认？[y/N] ')
        if confirm.lower() == 'y':
            restore(args.restore)
        else:
            print('已取消')
    else:
        backup()


if __name__ == '__main__':
    main()
