"""Пути приложения и аварийное логирование (без внешних зависимостей)."""

import os
import sys
import secrets
from datetime import datetime
from pathlib import Path

# =====================================================================================
#                    ПУТИ И ЛОГИ — до импорта модулей, чтобы log_error
#                    была доступна уже в первых блоках try/except
# =====================================================================================


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()


def user_data_dir() -> Path:
    """Единый каталог личных данных, отдельно от исходников приложения."""
    override = os.environ.get("KID_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        return Path(root) / "kid" if root else Path.home() / "AppData" / "Roaming" / "kid"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kid"


USER_DATA_DIR = user_data_dir()
CONFIG_PATH = str(USER_DATA_DIR / "config.json")
LOG_PATH = str(USER_DATA_DIR / "error.log")
BACKUP_DIR = USER_DATA_DIR / ".backups"
ENV_PATH = USER_DATA_DIR / ".env"


def relocated_user_file(path: str) -> str:
    """Старые ссылки в чатах продолжают открывать перенесённые документы."""
    if not path or os.path.exists(path):
        return path
    candidate = Path(path)
    if not candidate.is_absolute():
        return path
    try:
        relative = candidate.resolve().relative_to(Path(APP_DIR).resolve())
    except ValueError:
        return path
    if relative.parts and relative.parts[0] in {"generated_files", "data", "presets", "documents", "notes"}:
        target = USER_DATA_DIR / relative
        if target.is_file():
            return str(target)
    return path


# Переменные процесса имеют приоритет над пользовательским файлом окружения.
try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(ENV_PATH, override=False)


def initialize_user_storage() -> None:
    """Создать каталоги первого запуска, не изменяя существующие данные."""
    for name in ("history", "studio_history", "data", "generated_files", "presets", ".backups"):
        (USER_DATA_DIR / name).mkdir(parents=True, exist_ok=True)
    try:
        with ENV_PATH.open("x", encoding="utf-8") as stream:
            stream.write("SEARXNG_SECRET=" + secrets.token_hex(32) + "\n")
    except FileExistsError:
        pass
    if "load_dotenv" in globals():
        load_dotenv(ENV_PATH, override=False)


def log_error(text: str) -> None:
    try:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                "\n===== %s =====\n%s\n"
                % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
            )
    except Exception:
        pass
