"""Project and user-data backup archives with the existing restore layout."""

import os
import zipfile
from datetime import datetime


def create_project_backup(base_dir, backup_directory, user_data_dir, log_event) -> tuple[str, int]:
    """Создаёт ZIP-бэкап проекта и оставляет максимум десять последних архивов."""
    backup_dir = str(backup_directory)
    os.makedirs(backup_dir, exist_ok=True)
    filename = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.zip"
    archive_path = os.path.join(backup_dir, filename)
    # .next (dev-сервер Next.js держит в нём lock-файлы с запретом на чтение),
    # .venv и прочие служебные каталоги в архив не попадают.
    excluded_dirs = {
        ".backups",
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        ".cache",
        ".next",
        ".turbo",
        ".pnpm-store",
        ".pytest_cache",
        ".mypy_cache",
    }
    excluded_files = {".env"}
    # Конфиг, его снапшоты и история чатов живут ВНЕ папки проекта — в
    # %APPDATA%/kid, поэтому раньше в бэкап не попадали вообще. Без них
    # восстановление после сбоя невозможно: теряются профили, ключи,
    # настройки и вся переписка.
    system_files: list[tuple[str, str]] = []
    system_stats = {"config": 0, "snapshots": 0, "history": 0}
    try:
        from config_manager import CHAT_HISTORY_DIR as _history_dir
        from config_manager import CONFIG_PATH as _config_path
        from config_manager import CONFIG_SNAPSHOT_DIR as _snapshots_dir

        if os.path.isfile(_config_path):
            system_files.append((str(_config_path), "config.json"))
            system_stats["config"] = 1
        if os.path.isdir(_snapshots_dir):
            snapshot_names = sorted(
                name for name in os.listdir(_snapshots_dir) if name.endswith(".json")
            )
            for name in snapshot_names[-5:]:
                system_files.append(
                    (os.path.join(_snapshots_dir, name), f"_config_snapshots/{name}")
                )
                system_stats["snapshots"] += 1
        if os.path.isdir(_history_dir):
            for name in sorted(
                name for name in os.listdir(_history_dir) if name.endswith(".json")
            ):
                system_files.append(
                    (os.path.join(_history_dir, name), f"_history/{name}")
                )
                system_stats["history"] += 1
        log_event(
            "[BACKUP] Системные файлы: config=%d, снапшоты конфига=%d, история чатов=%d"
            % (
                system_stats["config"],
                system_stats["snapshots"],
                system_stats["history"],
            )
        )
    except Exception as exc:  # noqa: BLE001 - отсутствие конфига не рушит бэкап
        log_event(f"[BACKUP] Системные файлы не добавлены в архив: {exc}")
    # Новые данные сохраняются отдельно от исходников; старые секции архива
    # оставлены для совместимости с предыдущими версиями восстановления.
    for data_name in ("notes.json", "retrieval_metrics.json", ".env", "data",
                      "generated_files", "presets", "studio_history", "history",
                      "documents", "notes", "telegram.json", "telegram", "extensions"):
        data_path = user_data_dir / data_name
        data_files = [data_path] if data_path.is_file() else data_path.rglob("*")
        for data_file in data_files:
            if data_file.is_file() and not data_file.is_symlink():
                relative = data_file.relative_to(user_data_dir).as_posix()
                system_files.append((str(data_file), "_user_data/" + relative))
    skipped: list[str] = []
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, relative_name in system_files:
            try:
                archive.write(source, relative_name)
            except OSError as exc:
                skipped.append(f"{relative_name}: {exc}")
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [
                d
                for d in dirs
                if d not in excluded_dirs and not os.path.islink(os.path.join(root, d))
            ]
            relative_root = os.path.relpath(root, base_dir)
            for name in files:
                if name in excluded_files or name.endswith((".tmp", ".log")):
                    continue
                source = os.path.join(root, name)
                relative_name = os.path.normpath(os.path.join(relative_root, name))
                if relative_name == ".":
                    relative_name = name
                try:
                    archive.write(source, relative_name)
                except OSError as exc:
                    # Заблокированный/недоступный файл (например, .next/dev/lock
                    # у запущенного dev-сервера) не должен ронять весь бэкап.
                    skipped.append(f"{relative_name}: {exc}")
    if skipped:
        log_event(f"[BACKUP] Пропущено недоступных файлов: {len(skipped)}")
        for item in skipped[:5]:
            log_event(f"[BACKUP] Пропущен {item}")
    archives = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.startswith("backup_") and name.endswith(".zip")
    ]
    archives.sort(key=os.path.getmtime, reverse=True)
    for old_archive in archives[10:]:
        os.remove(old_archive)
    return filename, min(len(archives), 10)

