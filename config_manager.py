"""Безопасное хранение пользовательского конфига вне репозитория."""

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from app_paths import USER_DATA_DIR, initialize_user_storage

APP_NAME = "kid"
BASE_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_CONFIG_PATH = BASE_DIR / "config.default.json"
LEGACY_HISTORY_PATH = BASE_DIR / "sessions_history.json"
LEGACY_SESSIONS_META_PATH = BASE_DIR / "sessions_meta.json"
LEGACY_HISTORY_DIR = BASE_DIR / "history"
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")


def _config_dir() -> Path:
    return USER_DATA_DIR


CONFIG_DIR = _config_dir()
CONFIG_PATH = CONFIG_DIR / "config.json"
CHAT_HISTORY_DIR = CONFIG_DIR / "history"
#: Отдельное хранилище истории Document Studio — чаты студии не смешиваются
#: с основными чатами (свой каталог, свой список сессий).
STUDIO_HISTORY_DIR = CONFIG_DIR / "studio_history"
#: Каталог почасовых снапшотов config.json — страховка от потери профилей.
CONFIG_SNAPSHOT_DIR = CONFIG_DIR / "config_snapshots"
#: Журнал проблем с конфигом (повреждённый файл, подмена дефолтом, снапшоты).
CONFIG_ERROR_LOG = CONFIG_DIR / "config_errors.log"
#: Сколько последних снапшотов держим.
CONFIG_SNAPSHOT_LIMIT = 60
#: Минимальный интервал между снапшотами, сек (чтобы не забивать диск частыми записями).
CONFIG_SNAPSHOT_MIN_INTERVAL = 300
_CONFIG_WRITE_LOCK = threading.RLock()


def _history_filename(session_id: str) -> Path:
    """Возвращает безопасное имя файла для отдельного чата."""
    safe_id = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", str(session_id)).strip("_") or "default"
    return CHAT_HISTORY_DIR / (safe_id + ".json")


def load_chat_history(session_id: str) -> list:
    try:
        value = json.loads(_history_filename(session_id).read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_chat_history(session_id: str, history: list) -> None:
    if not isinstance(history, list):
        history = []
    CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    target = _history_filename(session_id)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def _safe_session_id(session_id: str) -> str:
    """Тот же нормализованный id, что использует _history_filename."""
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", str(session_id)).strip("_") or "default"


def purge_session_storage(session_id: str) -> list[str]:
    """ФИЗИЧЕСКИ стирает чат со всех уровней хранения.

    Удаляются:
    - отдельный файл истории чата и его .tmp (CHAT_HISTORY_DIR);
    - легаси-файл history/<sid>.json (LEGACY_HISTORY_DIR);
    - запись сессии в общем sessions_history.json (его пересобирает WEB_UI.pyw);
    - запись сессии в легаси sessions_meta.json.

    Без очистки общих файлов удаённый чат «воскресал»: _migrate_chat_storage()
    и /api/init заново поднимали сессию из legacy-хранилищ при следующем
    чтении конфига. Возвращает список удалённых объектов (для логов).
    """
    removed: list[str] = []
    safe = _safe_session_id(session_id)

    for path in (
        _history_filename(session_id),
        CHAT_HISTORY_DIR / (safe + ".json.tmp"),
        LEGACY_HISTORY_DIR / (safe + ".json"),
        LEGACY_HISTORY_DIR / (safe + ".json.tmp"),
    ):
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass

    def _drop_from(path: Path) -> None:
        try:
            if not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(session_id) in data:
                data.pop(str(session_id), None)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                removed.append(f"{path}#{session_id}")
        except (OSError, json.JSONDecodeError):
            pass

    _drop_from(LEGACY_HISTORY_PATH)
    _drop_from(LEGACY_SESSIONS_META_PATH)
    return removed


# --------------------------------------------------------------------------------------
# Document Studio: полностью отдельное хранилище чатов
# --------------------------------------------------------------------------------------


def _safe_studio_id(session_id: str) -> str:
    """Безопасный id для имени файла истории студии."""
    return (
        re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", str(session_id)).strip("_") or "default"
    )


def _studio_history_filename(session_id: str) -> Path:
    """Путь к файлу истории конкретного чата студии (отдельный каталог)."""
    return STUDIO_HISTORY_DIR / (_safe_studio_id(session_id) + ".json")


def load_studio_history(session_id: str) -> list:
    """История одного чата Document Studio."""
    try:
        value = json.loads(_studio_history_filename(session_id).read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_studio_history(session_id: str, history: list) -> None:
    """Атомарно сохраняет историю одного чата Document Studio."""
    if not isinstance(history, list):
        history = []
    STUDIO_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    target = _studio_history_filename(session_id)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def purge_studio_session_storage(session_id: str) -> list[str]:
    """Физически стирает чат студии (файл истории + временный файл)."""
    removed: list[str] = []
    safe = _safe_studio_id(session_id)
    for path in (
        _studio_history_filename(session_id),
        STUDIO_HISTORY_DIR / (safe + ".json.tmp"),
    ):
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
    return removed


def list_studio_history_ids() -> list[str]:
    """Id всех чатов студии, для которых есть файл истории."""
    try:
        return sorted(
            item.stem for item in STUDIO_HISTORY_DIR.glob("*.json") if item.is_file()
        )
    except OSError:
        return []



def _migrate_chat_storage(config: dict) -> dict:
    """Переносит старую общую историю в отдельные файлы пользовательских чатов."""
    history = config.get("sessions_history")
    if not isinstance(history, dict):
        history = {}
    metadata = config.get("sessions_meta")
    if not isinstance(metadata, dict):
        metadata = {}
        if LEGACY_SESSIONS_META_PATH.exists():
            try:
                old_metadata = json.loads(LEGACY_SESSIONS_META_PATH.read_text(encoding="utf-8"))
                if isinstance(old_metadata, dict):
                    metadata = old_metadata
            except (OSError, json.JSONDecodeError):
                pass
    migrated = False
    for session_id, messages in history.items():
        if isinstance(messages, list) and messages and not _history_filename(session_id).exists():
            save_chat_history(session_id, messages)
            migrated = True
    if LEGACY_HISTORY_PATH.exists():
        try:
            old = json.loads(LEGACY_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                for session_id, messages in old.items():
                    if isinstance(messages, list) and not _history_filename(session_id).exists():
                        save_chat_history(session_id, messages)
                        migrated = True
        except (OSError, json.JSONDecodeError):
            pass
    if LEGACY_HISTORY_DIR.exists():
        for legacy_file in LEGACY_HISTORY_DIR.glob("history_*.json"):
            try:
                messages = json.loads(legacy_file.read_text(encoding="utf-8"))
                session_id = "legacy_" + legacy_file.stem.removeprefix("history_")
                if isinstance(messages, list) and messages and not _history_filename(session_id).exists():
                    save_chat_history(session_id, messages)
                    metadata.setdefault(session_id, {"personaId": "main", "title": session_id, "timestamp": "Ранее"})
                    migrated = True
            except (OSError, json.JSONDecodeError):
                pass
    if "sessions_history" in config:
        config.pop("sessions_history", None)
        migrated = True
    if "sessions_meta" not in config or not isinstance(config.get("sessions_meta"), dict):
        config["sessions_meta"] = metadata
        migrated = True
    if migrated:
        save_config(config)
    for old_path in (LEGACY_HISTORY_PATH, LEGACY_SESSIONS_META_PATH):
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            pass
    return config


def _log_config_error(message: str) -> None:
    """Пишет проблему с конфигом в журнал (pythonw молчит, иначе её не увидеть)."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_ERROR_LOG.open("a", encoding="utf-8") as stream:
            stream.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def config_snapshot_paths() -> list[Path]:
    """Снапшоты config.json, отсортированные по времени (старые -> новые)."""
    try:
        return sorted(CONFIG_SNAPSHOT_DIR.glob("config-*.json"))
    except OSError:
        return []


def _profile_count(raw: str) -> int:
    """Сколько профилей в сыром JSON (0, если файл нечитаем)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return 0
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return len(profiles) if isinstance(profiles, dict) else 0


def snapshot_config(force: bool = False) -> Path | None:
    """Сохраняет копию текущего config.json в ``config_snapshots/``.

    Это страховка от потери профилей: даже если конфиг будет повреждён,
    удалён или залит дефолтом, последнее рабочее состояние останется на диске.

    Снапшоты троттлятся (не чаще ``CONFIG_SNAPSHOT_MIN_INTERVAL``), но состояние
    с БОЛЬШИМ числом профилей сохраняется всегда — иначе только что созданный
    профиль мог не попасть ни в один снапшот.
    """
    if not CONFIG_PATH.exists():
        return None
    try:
        payload = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _log_config_error("не удалось прочитать config.json для снапшота: %s" % exc)
        return None
    with _CONFIG_WRITE_LOCK:
        existing = config_snapshot_paths()
        if existing and not force:
            last = existing[-1]
            try:
                last_payload = last.read_text(encoding="utf-8")
                if last_payload == payload:
                    return last
                fresh = (
                    CONFIG_SNAPSHOT_MIN_INTERVAL > 0
                    and time.time() - last.stat().st_mtime < CONFIG_SNAPSHOT_MIN_INTERVAL
                )
                if fresh and _profile_count(payload) <= _profile_count(last_payload):
                    return last
            except OSError:
                pass
        try:
            CONFIG_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            target = CONFIG_SNAPSHOT_DIR / (
                "config-%s.json" % time.strftime("%Y%m%d-%H%M%S")
            )
            target.write_text(payload, encoding="utf-8")
        except OSError as exc:
            _log_config_error("не удалось сохранить снапшот конфига: %s" % exc)
            return None
        for stale in config_snapshot_paths()[: -CONFIG_SNAPSHOT_LIMIT]:
            try:
                stale.unlink()
            except OSError:
                pass
        return target


def _read_latest_snapshot() -> dict | None:
    for candidate in reversed(config_snapshot_paths()):
        try:
            return _read_json(candidate)
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _quarantine_corrupt_config() -> None:
    """Отводит нечитаемый config.json в сторону, чтобы он не затирался дефолтом."""
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = CONFIG_PATH.with_name("config.corrupt-%s.json" % stamp)
        os.replace(CONFIG_PATH, target)
        _log_config_error("повреждённый config.json сохранён как %s" % target.name)
    except OSError as exc:
        _log_config_error("не удалось отложить повреждённый config.json: %s" % exc)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    return value if isinstance(value, dict) else {}


def _default_config() -> dict:
    if DEFAULT_CONFIG_PATH.exists():
        return _read_json(DEFAULT_CONFIG_PATH)
    return {"active_profile": "main", "profiles": {"main": {"search_api_key": ""}}}


def _ensure_profile_fields(config: dict) -> dict:
    from research_controller import normalize_mode
    config["search_research_mode"] = normalize_mode(config.get("search_research_mode"))
    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        profiles = {"main": {}}
    if "search_api_key" not in config:
        first_profile = next(iter(profiles.values()), {})
        config["search_api_key"] = (first_profile.get("search_api_key") if isinstance(first_profile, dict) else "") or ""
    for name, profile in list(profiles.items()):
        profiles[name] = profile if isinstance(profile, dict) else {}
        # Удалённые режимы не должны возвращаться из старого конфига.
        for field in list(profiles[name]):
            if field == "is_coder" or field.startswith(("coder_", "architect_")):
                profiles[name].pop(field, None)
        if "search_api_key" not in profiles[name]:
            profiles[name]["search_api_key"] = config.get("search_api_key", "")
        limit_migrations = {
            "max_steps": (25, 60),
        }
        for field, (legacy_default, new_default) in limit_migrations.items():
            if field not in profiles[name] or profiles[name][field] == legacy_default:
                profiles[name][field] = new_default
        attach_defaults = {
            "attach_max_images": 4,
            "attach_max_text_chars": 60000,
            "attach_max_text_per_file": 60000,
            "attach_max_total_text": 180000,
            "attach_csv_max_rows": 400,
            "attach_xlsx_max_rows": 400,
            # 0 = без клиентской обрезки отдельного сообщения (вложения и запрос
            # доходят целиком; ограничиваются только attach_max_total_text).
            "chat_msg_max_chars": 0,
            "chat_msg_text_part_max_chars": 0,
        }
        for field, default in attach_defaults.items():
            profiles[name].setdefault(field, default)
    config["profiles"] = profiles
    if config.get("active_profile") not in profiles:
        config["active_profile"] = next(iter(profiles))
    return config


def _initialize_config() -> None:
    initialize_user_storage()
    if CONFIG_PATH.exists():
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Пропавший config.json — не повод молча отдать дефолт (так терялись профили):
    # сначала пробуем последний снапшот.
    recovered = _read_latest_snapshot()
    if recovered is not None:
        _log_config_error(
            "config.json отсутствовал — восстановлен из последнего снапшота"
        )
        save_config(_ensure_profile_fields(recovered), snapshot=False)
        return
    source = DEFAULT_CONFIG_PATH
    if source.exists():
        data = _read_json(source)
    else:
        data = _default_config()
    _log_config_error(
        "config.json отсутствовал, снапшотов нет — создан из %s" % source.name
    )
    save_config(_ensure_profile_fields(data))


def load_config() -> dict:
    """Загрузить рабочий конфиг из системной директории пользователя."""
    _initialize_config()
    try:
        config = _read_json(CONFIG_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        # Раньше здесь молча возвращался дефолт, и первая же запись (заметки,
        # активная сессия, настройки прокси) затирала профили пользователя.
        # Теперь повреждённый файл уходит в карантин, а конфиг поднимается
        # из последнего снапшота.
        _log_config_error("config.json не читается (%s)" % exc)
        _quarantine_corrupt_config()
        recovered = _read_latest_snapshot()
        if recovered is None:
            _log_config_error(
                "снапшотов нет — работаем на дефолтном конфиге без перезаписи профилей"
            )
            config = _default_config()
        else:
            _log_config_error("config.json восстановлен из снапшота")
            config = recovered
            # Возвращаем восстановленный файл на место, чтобы следующее чтение
            # не уходило в ту же ветку восстановления.
            save_config(_ensure_profile_fields(recovered), snapshot=False)
    config = _ensure_profile_fields(config)
    return _migrate_chat_storage(config)


def save_config(data: dict, snapshot: bool = True) -> None:
    """Атомарно сохранить конфиг рядом с рабочим файлом.

    snapshot=True дополнительно кладёт копию предыдущего состояния в
    ``config_snapshots/`` — страховка от потери профилей.
    """
    if not isinstance(data, dict):
        raise TypeError("config data must be a dict")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _CONFIG_WRITE_LOCK:
        if snapshot:
            # Состояние ДО записи: если новая версия окажется «беднее»
            # (например, кто-то сохранит частичный конфиг), профили не пропадут.
            snapshot_config()
        payload = _ensure_profile_fields(copy.deepcopy(data))
        temporary_path = CONFIG_PATH.with_name(
            CONFIG_PATH.name + ".%s.%s.tmp" % (os.getpid(), threading.get_ident())
        )
        for attempt in range(8):
            try:
                with temporary_path.open("w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, CONFIG_PATH)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(0.05 * (attempt + 1))
        if snapshot:
            # Состояние ПОСЛЕ записи: фиксируем расширение набора профилей сразу,
            # не дожидаясь следующего сохранения (троттлинг пропустит равные версии).
            snapshot_config()


def get_active_profile() -> dict:
    config = load_config()
    profile = config["profiles"].get(config["active_profile"], {})
    return dict(profile) if isinstance(profile, dict) else {}


def update_profile_key(profile_name: str, key: str, value) -> None:
    config = load_config()
    profiles = config.setdefault("profiles", {})
    profile = profiles.setdefault(profile_name, {})
    profile[key] = value
    save_config(config)
