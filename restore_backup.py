# -*- coding: utf-8 -*-
"""Восстановление системных файлов из ZIP-бэкапа проекта.

Конфиг, история чатов и снапшоты конфига лежат ВНЕ папки проекта
(%APPDATA%/kid), поэтому в архиве они лежат в служебных каталогах:

    config.json                  -> %APPDATA%/kid/config.json
    _config_snapshots/*.json     -> %APPDATA%/kid/config_snapshots/*.json
    _history/*.json              -> %APPDATA%/kid/history/*.json

Использование (по умолчанию — только план, ничего не пишется):

    python restore_backup.py                       # последний бэкап: что будет восстановлено
    python restore_backup.py --apply               # восстановить (история не перезаписывается)
    python restore_backup.py --apply --force       # перезаписать и существующую историю
    python restore_backup.py --archive "%APPDATA%/kid/.backups/backup_2026-10-10_08-20-00.zip" --apply

Перед записью текущий config.json уходит в config_snapshots (см. config_manager),
так что восстановление тоже обратимо.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import config_manager as cm  # noqa: E402
from app_paths import BACKUP_DIR  # noqa: E402


def latest_archive() -> str:
    backup_dir = str(BACKUP_DIR)
    if not os.path.isdir(backup_dir):
        raise SystemExit("Каталог .backups не найден — бэкапов нет.")
    candidates = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.startswith("backup_") and name.endswith(".zip")
    ]
    if not candidates:
        raise SystemExit("В .backups нет ни одного архива.")
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Восстановление config.json и истории чатов из бэкапа.")
    parser.add_argument("--archive", help="Путь к ZIP-бэкапу (по умолчанию — самый свежий).")
    parser.add_argument("--apply", action="store_true", help="Реально записать файлы (без флага — только план).")
    parser.add_argument("--force", action="store_true", help="Перезаписывать существующие файлы истории и снапшотов.")
    args = parser.parse_args()

    archive_path = args.archive or latest_archive()
    if not os.path.isfile(archive_path):
        raise SystemExit("Архив не найден: %s" % archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if "config.json" not in names:
            raise SystemExit(
                "В архиве нет config.json — он собран старой версией бэкапа "
                "(до того, как системные файлы стали попадать в архив)."
            )
        raw_config = archive.read("config.json").decode("utf-8")
        config = json.loads(raw_config)
        history_names = [n for n in names if n.startswith("_history/") and n.endswith(".json")]
        snapshot_names = [n for n in names if n.startswith("_config_snapshots/") and n.endswith(".json")]
        history_payloads = {os.path.basename(n): archive.read(n) for n in history_names}
        snapshot_payloads = {os.path.basename(n): archive.read(n) for n in snapshot_names}
        user_payloads = {}
        for name in names:
            if not name.startswith("_user_data/") or name.endswith("/"):
                continue
            relative = PurePosixPath(name.removeprefix("_user_data/"))
            if relative.is_absolute() or any(part in {"..", "."} or ":" in part or "\\" in part for part in relative.parts):
                raise SystemExit("Небезопасный путь данных в архиве")
            if not relative.parts or relative.parts[0] not in {
                "notes.json", "retrieval_metrics.json", ".env", "data",
                "generated_files", "presets", "studio_history", "history", "documents", "notes",
            }:
                raise SystemExit("Неизвестный каталог данных в архиве")
            target = (cm.CONFIG_DIR / Path(*relative.parts)).resolve()
            if not target.is_relative_to(cm.CONFIG_DIR.resolve()):
                raise SystemExit("Путь данных выходит за пределы каталога пользователя")
            user_payloads[target] = archive.read(name)

    profiles = list(config.get("profiles", {}))
    print("Архив:            %s" % archive_path)
    print("Каталог конфига:  %s" % cm.CONFIG_DIR)
    print("Профили в архиве: %s" % (", ".join(profiles) if profiles else "нет"))
    print("Активный профиль: %s" % config.get("active_profile"))
    print("История чатов:    %d файл(ов)" % len(history_payloads))
    print("Снапшоты конфига: %d файл(ов)" % len(snapshot_payloads))
    print("Другие данные:   %d файл(ов)" % len(user_payloads))

    if not args.apply:
        print("\nЭто только план. Добавьте --apply, чтобы записать файлы.")
        return 0

    # Текущее состояние не теряем: снапшот перед подменой.
    saved = cm.snapshot_config(force=True)
    if saved:
        print("\nТекущий config.json сохранён в %s" % saved.name)

    cm.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cm.save_config(config, snapshot=False)
    print("Восстановлен config.json (%d профилей)" % len(profiles))

    os.makedirs(cm.CHAT_HISTORY_DIR, exist_ok=True)
    restored_history = 0
    for name, payload in history_payloads.items():
        target = os.path.join(cm.CHAT_HISTORY_DIR, name)
        if os.path.exists(target) and not args.force:
            continue
        with open(target, "wb") as stream:
            stream.write(payload)
        restored_history += 1
    print("Восстановлено чатов: %d из %d" % (restored_history, len(history_payloads)))

    if snapshot_payloads:
        os.makedirs(cm.CONFIG_SNAPSHOT_DIR, exist_ok=True)
        restored_snapshots = 0
        for name, payload in snapshot_payloads.items():
            target = os.path.join(cm.CONFIG_SNAPSHOT_DIR, name)
            if os.path.exists(target) and not args.force:
                continue
            with open(target, "wb") as stream:
                stream.write(payload)
            restored_snapshots += 1
        print("Восстановлено снапшотов конфига: %d" % restored_snapshots)

    restored_data = 0
    for target, payload in user_payloads.items():
        if target.exists() and not args.force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        restored_data += 1
    print("Восстановлено файлов данных: %d" % restored_data)

    print("\nГотово. Перезапустите приложение, чтобы оно подняло восстановленный конфиг.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
