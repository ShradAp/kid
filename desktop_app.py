"""
Запуск автономного десктопного окна Windows с интерфейсом v0.
Мост бэкенда и запуск окна объединены в одном файле.
"""

import collections
import email.parser
import email.policy
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

# Проверка зависимостей ДО их импорта: без неё запуск в окружении без пакетов
# падал сырым «ModuleNotFoundError: No module named 'keyboard'» вместо подсказки.
_REQUIRED_MODULES = (
    "keyboard",      # глобальные хоткеи
    "pystray",       # иконка трея
    "webview",       # окно приложения (pywebview)
    "PIL",           # pillow: картинки и иконка трея
    "requests",      # api_client, поиск, network_agent
    "curl_cffi",     # network_agent/core: TLS-слой с impersonate
    "httpx",         # network_agent/client
    "dotenv",        # network_agent (python-dotenv)
    "playwright",    # network_agent/browser
)
_missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
if _missing:
    _requirements = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    _message = (
        "Не установлены зависимости приложения: " + ", ".join(_missing) + "\n"
        f"Установите их в ТЕКУЩИЙ интерпретатор ({sys.executable}):\n"
        f'    "{sys.executable}" -m pip install -r "{_requirements}"\n'
        "Либо запустите приложение интерпретатором, где пакеты уже есть."
    )
    try:
        print(_message)
    except Exception:
        pass
    try:
        input("Нажмите Enter для выхода...")
    except Exception:
        pass
    sys.exit(1)

from tkinter import filedialog
import tkinter as tk
from tkinter import filedialog

import keyboard
import pystray
import requests
import webview
from PIL import Image, ImageDraw

import api_client
import attach_tools
import file_tools
import search_tools
import retrieval_quality
import tool_gating
from app_paths import BACKUP_DIR, USER_DATA_DIR, relocated_user_file
from bridge_security import BRIDGE_TOKEN, FileAccess, authorized, retained_secret, request_length
FILE_ACCESS = FileAccess((USER_DATA_DIR / "generated_files", Path(tempfile.gettempdir()) / "kid_uploads"))
from config_manager import (
    load_chat_history,
    load_config,
    load_studio_history,
    purge_session_storage,
    purge_studio_session_storage,
    save_chat_history,
    save_config,
    save_studio_history,
)

# Локальная память профиля: LanceDB в каталоге данных пользователя.
# + FastEmbed, факты берутся только из диалогов. Импорт защищён: без lancedb или
# fastembed приложение работает как раньше — модуль сам переключается на локальное
# JSON-хранилище, а причина отказа видна в логе (MEMORY_IMPORT_ERROR).
try:
    from memory import user_profile as user_profile_memory

    MEMORY_IMPORT_ERROR = ""
except Exception as _memory_import_error:  # noqa: BLE001
    user_profile_memory = None
    MEMORY_IMPORT_ERROR = (
        f"{type(_memory_import_error).__name__}: {_memory_import_error}"
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Кольцевой буфер последних событий бэкенда — читается фронтендом через /api/logs
LOG_BUFFER: "collections.deque[str]" = collections.deque(maxlen=500)

# Живой прогресс текущей генерации по сессиям: фронтенд опрашивает
# GET /api/progress?session_id=..., чтобы плашка «Thinking process» показывала
# вызовы инструментов в реальном времени. Регистрируется при старте /api/chat,
# снимается после отправки ответа (финальный список по-прежнему возвращается
# в теле ответа как thinking[]).
LIVE_THINKING: "dict[str, list]" = {}

# Живой поток «размышлений» модели (reasoning content / thought parts):
# накапливается по мере стриминга и уходит в GET /api/progress отдельным полем
# reasoning, чтобы виджет Thinking process показывал связный текст рассуждений
# в реальном времени, пока формируется финальный ответ.
LIVE_REASONING: "dict[str, str]" = {}

# События отмены генерации по сессиям: POST /api/stop выставляет событие,
# а циклы /api/chat (обычный и ассистент) проверяют его между шагами; долгие
# вызовы LLM перестают ожидаться (см. _call_chat_cancellable). Регистрируется
# при старте /api/chat, снимается вместе с LIVE_THINKING.
CANCEL_EVENTS: "dict[str, threading.Event]" = {}


class _GenerationStopped(Exception):
    """Внутренний сигнал: пользователь нажал «стоп» во время генерации /api/chat."""


def _call_chat_cancellable(cancel_event, timeout, **kwargs):
    """api_client.call_chat_api с отменой по событию.

    Прервать requests посреди чтения ответа нельзя, поэтому сам HTTP-запрос
    крутится в потоке-демоне, а вызывающий поток ожидает результат опросом
    события отмены. При «стопе» ожидание прекращается мгновенно, поток запроса
    бросается (daemon) — его ответ будет отброшен. Возврат None = отменено.
    В стриминговом режиме (stream=True) cancel_event дополнительно передаётся в
    api_client, чтобы чтение SSE-чанков прерывалось между порциями.
    """
    box: dict = {}

    def _worker():
        try:
            box["result"] = api_client.call_chat_api(
                timeout=timeout, cancel_event=cancel_event, **kwargs
            )
        except BaseException as exc:  # noqa: BLE001 — пробрасываем в вызывающий поток
            box["error"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(timeout=0.2)
        if cancel_event is not None and cancel_event.is_set():
            return None
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _collapse_ws(text):
    """Схлопывает пробелы и переносы строк для сравнения текстов."""
    return " ".join((text or "").split())


# Не вырезаем короткие «размышления»: это защита от ложных срабатываний,
# когда реальный ответ случайно начинается с тех же нескольких слов.
_MIN_REASONING_STRIP_CHARS = 60


def _strip_reasoning_from_reply(final_reply, reasoning_acc):
    """Вырезает из видимого ответа продублированные моделью размышления.

    Модели с нативным thinking (например, google/gemini-3.8-flash) иногда
    выдают цепочку рассуждений сразу в двух местах: в reasoning_content
    (уходит в блок «Thinking process») и в content (становится видимым
    ответом). Тогда ответ буквально повторяет размышления — убираем это
    повторение, оставляя только реальный текст.
    """
    if not final_reply or not reasoning_acc:
        return final_reply
    reason_norm = _collapse_ws(reasoning_acc)
    if len(reason_norm) < _MIN_REASONING_STRIP_CHARS:
        return final_reply
    reply_norm = _collapse_ws(final_reply)
    # Ответ целиком состоит из размышлений — видимого текста нет.
    if reply_norm == reason_norm:
        return ""
    # Размышления продублированы перед реальным ответом — отрезаем префикс.
    if reply_norm.startswith(reason_norm):
        stripped = (final_reply or "").strip()
        reason_stripped = (reasoning_acc or "").strip()
        if reason_stripped and stripped.startswith(reason_stripped):
            return stripped[len(reason_stripped):].strip()
        # Форматирование разошлось (переносы/пробелы) — режем по
        # нормализованному префиксу как запасной вариант.
        return reply_norm[len(reason_norm):].strip()
    return final_reply

# Opener БЕЗ прокси: urllib на Windows подхватывает системный прокси из
# реестра (ProxyServer/WinINET), который может быть мёртвым или требовать
# авторизацию — тогда /api/models ломался на «HTTP 407: Proxy Authentication
# Required». LLM-клиент (requests) системный прокси и так не читает — ходим
# напрямую и здесь.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log_event(msg: str) -> None:
    """Печатает сообщение в консоль и сохраняет его в буфер для панели логов фронтенда."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        # Логирование никогда не должно ронять обработчик запроса: падение print
        # (pythonw без консоли, ограничения кодировки) обрывало ответ /api/chat,
        # и финальный ответ ассистента не доходил до фронтенда.
        print(line)
    except Exception:
        pass
    try:
        LOG_BUFFER.append(line)
    except Exception:
        pass


def create_project_backup() -> tuple[str, int]:
    """Создаёт ZIP-бэкап проекта и оставляет максимум десять последних архивов."""
    backup_dir = str(BACKUP_DIR)
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
                      "documents", "notes"):
        data_path = USER_DATA_DIR / data_name
        data_files = [data_path] if data_path.is_file() else data_path.rglob("*")
        for data_file in data_files:
            if data_file.is_file() and not data_file.is_symlink():
                relative = data_file.relative_to(USER_DATA_DIR).as_posix()
                system_files.append((str(data_file), "_user_data/" + relative))
    skipped: list[str] = []
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, relative_name in system_files:
            try:
                archive.write(source, relative_name)
            except OSError as exc:
                skipped.append(f"{relative_name}: {exc}")
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [
                d
                for d in dirs
                if d not in excluded_dirs and not os.path.islink(os.path.join(root, d))
            ]
            relative_root = os.path.relpath(root, BASE_DIR)
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


def _word_presets_payload() -> dict:
    """Пресеты оформления из services/word_automation/presets (для Document Studio)."""
    try:
        from services.word_automation.preset_schema import list_presets, load_preset
    except Exception as exc:  # noqa: BLE001 - модуль может быть не установлен
        return {"presets": [], "error": f"word_automation недоступен: {exc}"}
    presets: list[dict] = []
    for name in list_presets():
        try:
            preset = load_preset(name)
        except Exception as exc:  # noqa: BLE001 - битый пресет не рушит список
            log_event(f"[!] Пресет {name} не загружен: {exc}")
            continue
        data = preset.model_dump(mode="json")
        data["id"] = preset.name
        presets.append(data)
    return {"presets": presets}


def _uploaded_document_path(reference: str) -> str | None:
    """Путь к загруженному документу: абсолютный путь либо file_id из /api/upload."""
    reference = relocated_user_file((reference or "").strip())
    if not reference:
        return None
    if FILE_ACCESS.allowed(reference):
        return reference
    upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
    if os.path.isdir(upload_dir):
        for name in os.listdir(upload_dir):
            if name == reference or name.startswith(reference + "_"):
                candidate = os.path.join(upload_dir, name)
                if os.path.isfile(candidate):
                    return candidate
    return None


def _word_compile_payload(req_data: dict) -> dict:
    """Собирает новый DOCX из блоков либо нормализует приложенный файл."""
    from services.word_automation import DocxFormatter

    preset = str(req_data.get("preset") or "").strip() or None
    source_reference = str(req_data.get("source_file") or "").strip()
    formatter = DocxFormatter()
    if source_reference:
        source_path = _uploaded_document_path(source_reference)
        if not source_path:
            raise FileNotFoundError(f"Исходный документ не найден: {source_reference}")
        output_path = formatter.normalize_document(source_path, preset=preset)
    else:
        blocks = req_data.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("Передайте blocks или source_file")
        output_path = formatter.create_document(blocks, preset=preset)
    name = os.path.basename(output_path)
    return {
        "ok": True,
        "path": output_path,
        "filename": name,
        "stats": dict(formatter.last_stats),
        "download": "/api/download-local?path=%s&name=%s"
        % (urllib.parse.quote(output_path), urllib.parse.quote(name)),
    }


#: Рабочие документы Document Studio: session_id -> {path, name, preset}.
#: Файл живёт на бэкенде с момента прикрепления — модель обращается к нему через
#: инструменты, не получая его как обычное вложение чата.
STUDIO_DOCS: dict[str, dict] = {}
STUDIO_LOCK = threading.Lock()

#: Имена агентских инструментов редактора DOCX (схема api_client.WORD_TOOLS_SCHEMA).
_WORD_TOOL_NAMES = {
    (tool.get("function") or {}).get("name")
    for tool in getattr(api_client, "WORD_TOOLS_SCHEMA", [])
    if isinstance(tool, dict)
}

#: Подсказка для модели в чате профиля документов (добавляется к системному промпту).
_STUDIO_HINT = (
    "[Document Studio: к студии прикреплён рабочий DOCX. Ты правишь именно его. "
    "Доступные инструменты: get_document_state (показать состояние), set_page_margins, "
    "set_paragraph_format, set_font, format_tables (выравнивание/шрифт/отступы таблиц), "
    "align_figures, reset_spacing (интервалы в 0), apply_document_preset, "
    "apply_document_rules; для анализа оформления — get_document_preset (приложенные к чату "
    "пресеты), get_preset (сохранённый пресет по имени), list_available_presets. "
    "Отдельные области: title_page (титульный лист — шапка/заголовок/подписи/подвал не "
    "наследуют абзацный отступ основного текста), special_sections (реферат/задание — "
    "название по центру, текст по ширине), page_numbering (нумерация страниц: колонтитул, "
    "старт, скрытие на титульнике/задании). "
    "Правки применяются к файлу сразу и обновляют предпросмотр по центру. "
    "Сначала при необходимости вызови get_document_state, затем действуй.]"
)


def _studio_state(session_id: str) -> dict:
    """Возвращает (создавая при необходимости) состояние документа студии."""
    with STUDIO_LOCK:
        state = STUDIO_DOCS.get(session_id)
        if state is None:
            state = {
                "path": "", "name": "", "preset": "",
                "preset_json": None, "attached_presets": [],
            }
            STUDIO_DOCS[session_id] = state
        state.setdefault("attached_presets", [])
        return state


def _studio_register(
    session_id: str,
    path: str = "",
    name: str = "",
    preset_name: str = "",
    preset_json: object = None,
) -> None:
    """Обновляет состояние документа студии, не теряя активный пресет."""
    with STUDIO_LOCK:
        state = STUDIO_DOCS.get(session_id) or {
            "path": "", "name": "", "preset": "", "preset_json": None,
            "attached_presets": [],
        }
        if path:
            state["path"] = path
        if name:
            state["name"] = name
        if preset_name:
            state["preset"] = preset_name
        if preset_json is not None:
            state["preset_json"] = preset_json
        state.setdefault("attached_presets", [])
        STUDIO_DOCS[session_id] = state


def _studio_set_preset(session_id: str, preset_json: object) -> None:
    """Запоминает активный пресет оформления для чата студии."""
    _studio_register(session_id, preset_json=preset_json)


def _studio_set_presets(session_id: str, presets_json: list) -> None:
    """Запоминает список пресетов, приложенных к чату (их может быть несколько)."""
    with STUDIO_LOCK:
        # Лок берётся ровно один раз: STUDIO_LOCK нереентерабельный,
        # поэтому _studio_state() здесь вызывать нельзя (был бы дедлок).
        state = STUDIO_DOCS.get(session_id)
        if state is None:
            state = {
                "path": "", "name": "", "preset": "",
                "preset_json": None, "attached_presets": [],
            }
            STUDIO_DOCS[session_id] = state
        state["attached_presets"] = list(presets_json or [])
        if presets_json:
            state["preset_json"] = presets_json[-1]


def _download_url(path: str, name: str) -> str:
    """Ссылка на скачивание локального файла через мост."""
    return "/api/download-local?path=%s&name=%s" % (
        urllib.parse.quote(path),
        urllib.parse.quote(name),
    )


def _content_disposition(filename: str) -> str:
    """Заголовок Content-Disposition для имени файла (в т.ч. кириллического).

    HTTP-заголовки кодируются latin-1, поэтому кириллица в имени рушила
    ``send_header`` (UnicodeEncodeError) и соединение обрывалось — браузер
    показывал «Failed to fetch»/ERR_EMPTY_RESPONSE. Даём ASCII-фолбэк в
    ``filename`` и корректное имя по RFC 5987 в ``filename*``.
    """
    safe = str(filename or "download").replace('"', "_").replace("\\", "_")
    ascii_name = safe.encode("ascii", "ignore").decode("ascii").strip() or "download"
    quoted = urllib.parse.quote(safe, safe="")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quoted)


def _generated_file(file_id: str) -> tuple[str | None, str | None]:
    """Путь и имя для скачивания файла, созданного file_tools.create_downloadable_file.

    Возвращает (полный_путь, имя_для_скачивания) либо (None, None), если файл
    по переданному id (rab://download/<id>) не найден.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return None, None
    try:
        index = file_tools._load_index()
    except Exception:  # noqa: BLE001 - битый index.json не роняет выдачу файла
        return None, None
    entry = index.get(file_id) if isinstance(index, dict) else None
    if not isinstance(entry, dict):
        return None, None
    stored_name = str(entry.get("stored_name") or "")
    download_name = str(entry.get("download_name") or "") or stored_name
    if not stored_name:
        return None, None
    path = os.path.join(file_tools.GENERATED_DIR, stored_name)
    if not FILE_ACCESS.allowed(path):
        return None, None
    return path, download_name


def _studio_reference(req_data: dict) -> str:
    """Ссылка на документ: явный путь/file_id либо текущий файл сессии."""
    reference = str(
        req_data.get("source_file") or req_data.get("file_id") or ""
    ).strip()
    if reference:
        return reference
    session_id = str(req_data.get("session_id") or "document-studio")
    return str(_studio_state(session_id).get("path") or "")


def _word_analyze_payload(req_data: dict) -> dict:
    """Анализирует DOCX и отдаёт предпросмотр + отчёт БЕЗ создания пресета.

    Пресет больше НЕ создаётся автоматически: раньше каждый анализ писал новый
    файл в ``services/word_automation/presets``, поэтому каталог разрастался
    дубликатами вида ``*_2.json`` и в UI появлялись «фантомные» пресеты.
    Сохранение теперь явное — через ``/api/word/preset-save`` либо по флагу
    ``save_preset`` / явно заданному ``preset_name``.

    Результат анализа остаётся в состоянии сессии (``preset_json``), чтобы
    предпросмотр и модель могли им пользоваться без записи на диск.
    """
    from services.word_automation import analyze_document

    reference = _studio_reference(req_data)
    source_path = _uploaded_document_path(reference)
    if not source_path:
        raise FileNotFoundError(f"Документ не найден: {reference or 'не передан'}")
    session_id = str(req_data.get("session_id") or "document-studio")
    name = str(req_data.get("preset_name") or "").strip() or None
    result = analyze_document(source_path, name=name)

    wants_save = bool(req_data.get("save_preset")) or bool(name)
    saved = ""
    if wants_save:
        try:
            from services.word_automation import save_preset

            saved = save_preset(result.preset)
        except Exception as exc:  # noqa: BLE001 - неудачное сохранение не рушит анализ
            log_event(f"[!] Не удалось сохранить пресет '{result.preset.name}': {exc}")

    _studio_register(
        session_id,
        path=source_path,
        name=os.path.basename(source_path),
        # Имя пресета регистрируем только если он реально лежит на диске,
        # иначе apply_preset(preset="<имя>") не найдёт файл.
        preset_name=result.preset.name if saved else "",
    )
    if not saved:
        # _studio_register не трогает поле при пустой строке, поэтому чистим
        # вручную: имя пресета предыдущего документа к новому не относится.
        with STUDIO_LOCK:
            state = STUDIO_DOCS.get(session_id)
            if state is not None:
                state["preset"] = ""
    preset_data = result.preset.model_dump(mode="json")
    preset_data["id"] = result.preset.name
    _studio_set_preset(session_id, preset_data)
    return {
        "ok": True,
        "preset": preset_data,
        "preview": result.preview,
        "report": result.report,
        "preset_path": saved,
        "preset_saved": bool(saved),
        "source_name": os.path.basename(source_path),
    }


def _word_preset_save_payload(req_data: dict) -> dict:
    """Явно извлекает и сохраняет пресет оформления документа студии.

    Вызывается действием пользователя («Извлечь пресет»): принимает либо готовый
    JSON пресета из UI, либо сам анализирует документ. Это единственное место,
    где пресет записывается на диск.
    """
    from services.word_automation import save_preset

    session_id = str(req_data.get("session_id") or "document-studio")
    name = str(req_data.get("preset_name") or "").strip() or None

    preset_json = req_data.get("preset")
    if isinstance(preset_json, dict) and preset_json:
        from services.word_automation.preset_schema import StylePreset

        preset = StylePreset.model_validate(preset_json)
        if name:
            preset.name = name
    else:
        from services.word_automation import analyze_document

        reference = _studio_reference(req_data)
        source_path = _uploaded_document_path(reference)
        if not source_path:
            raise FileNotFoundError(
                f"Документ не найден: {reference or 'не передан'}"
            )
        preset = analyze_document(source_path, name=name).preset

    saved = save_preset(preset)
    preset_data = preset.model_dump(mode="json")
    preset_data["id"] = preset.name
    _studio_register(session_id, preset_name=preset.name)
    _studio_set_preset(session_id, preset_data)
    log_event(f"[+] Пресет '{preset.name}' извлечён пользователем -> {saved}")
    return {
        "ok": True,
        "preset": preset_data,
        "preset_path": saved,
        "preset_saved": True,
        "name": preset.name,
    }


def _word_state_payload(req_data: dict) -> dict:
    """Возвращает состояние студии: открытый файл, пресет и предпросмотр.

    Фронтенд вызывает его при возврате в Document Studio после перехода между
    разделами: файл и пресет живут на бэкенде, поэтому документ, анализ и
    предпросмотр восстанавливаются без повторной загрузки пользователем.
    """
    session_id = str(req_data.get("session_id") or "document-studio")
    state = _studio_state(session_id)
    path = str(state.get("path") or "")
    name = str(state.get("name") or "") or (os.path.basename(path) if path else "")
    payload: dict = {
        "ok": True,
        "session_id": session_id,
        "path": path,
        "name": name,
        "preset": str(state.get("preset") or ""),
        "preset_json": state.get("preset_json"),
        "attached_presets": list(state.get("attached_presets") or []),
        "has_document": bool(path and os.path.isfile(path)),
    }
    if payload["has_document"]:
        payload["download"] = _download_url(path, name)
        try:
            from services.word_automation import preview_document

            payload["preview"] = preview_document(path)
        except Exception as exc:  # noqa: BLE001 - предпросмотр не критичен
            log_event(f"[!] Предпросмотр студии не построен: {exc}")
    return payload


def _word_close_payload(req_data: dict) -> dict:
    """Закрывает документ студии: сбрасывает файл, пресет и анализ сессии.

    Явное действие «Закрыть документ» — без него состояние сессии жило бы до
    перезапуска моста, и следующий документ «наследовал» бы прошлый пресет.
    """
    session_id = str(req_data.get("session_id") or "document-studio")
    with STUDIO_LOCK:
        state = STUDIO_DOCS.pop(session_id, None)
    closed_name = str((state or {}).get("name") or "")
    log_event(f"[+] Документ студии закрыт: {closed_name or '(пусто)'}")
    return {
        "ok": True,
        "closed": closed_name,
        "path": "",
        "name": "",
        "preset": "",
        "preset_json": None,
        "attached_presets": [],
        "has_document": False,
    }


def _word_preview_payload(req_data: dict) -> dict:
    """Возвращает модель предпросмотра текущего документа студии."""
    from services.word_automation import preview_document

    reference = _studio_reference(req_data)
    source_path = _uploaded_document_path(reference)
    if not source_path:
        raise FileNotFoundError(f"Документ не найден: {reference or 'не передан'}")
    return {
        "ok": True,
        "preview": preview_document(source_path),
        "source_name": os.path.basename(source_path),
    }


def _word_apply_payload(req_data: dict) -> dict:
    """Применяет пресет и/или список правил к документу студии (in-place)."""
    from services.word_automation import apply_preset, apply_rules, preview_document

    session_id = str(req_data.get("session_id") or "document-studio")
    state = _studio_state(session_id)
    reference = _studio_reference(req_data)
    source_path = _uploaded_document_path(reference)
    if not source_path:
        raise FileNotFoundError(f"Документ не найден: {reference or 'не передан'}")

    preset = req_data.get("preset")
    rules = req_data.get("rules")
    stats: dict = {}
    if preset:
        outcome = apply_preset(source_path, preset)
        stats.update(outcome.get("stats") or {})
        if isinstance(preset, dict):
            state["preset"] = str(preset.get("name") or state.get("preset") or "")
            _studio_set_preset(session_id, preset)
        elif isinstance(preset, str):
            state["preset"] = preset
            try:
                from services.word_automation import load_preset

                loaded = load_preset(preset)
                stored = loaded.model_dump(mode="json")
                stored["id"] = loaded.name
                _studio_set_preset(session_id, stored)
            except Exception:  # noqa: BLE001 - имя могло не найтись, это не критично
                pass
    elif rules:
        outcome = apply_rules(source_path, rules, output_path=source_path)
        stats.update(outcome.get("stats") or {})
    else:
        raise ValueError("Передайте preset или rules")

    target = outcome.get("path", source_path)
    _studio_register(
        session_id, target, os.path.basename(target), str(state.get("preset") or "")
    )
    filename = os.path.basename(target)
    return {
        "ok": True,
        "path": target,
        "filename": filename,
        "stats": stats,
        "preview": preview_document(target),
        "download": _download_url(target, filename),
    }


def _run_word_tool(fn_name: str, fn_args: dict, session_id: str) -> str:
    """Исполняет инструмент редактирования DOCX и возвращает текст модели.

    Все правки физически применяются к файлу студии (in-place) и сразу видны в
    центральном предпросмотре после обновления страницы.
    """
    from services.word_automation import (
        PresetError,
        apply_preset,
        apply_rules,
        preview_document,
    )

    fn_args = fn_args or {}
    state = _studio_state(session_id)

    # Инструменты анализа пресетов работают и без прикреплённого документа.
    if fn_name == "list_available_presets":
        from services.word_automation.preset_schema import list_presets

        names = list_presets()
        return (
            "Доступные пресеты оформления: " + ", ".join(names)
            if names
            else "Сохранённых пресетов оформления нет."
        )

    if fn_name == "get_document_preset":
        attached = state.get("attached_presets") or []
        if attached:
            return (
                "Пресеты оформления, приложенные к чату (JSON):\n"
                + json.dumps(attached, ensure_ascii=False)
            )
        active = state.get("preset_json")
        if not active:
            active_name = str(state.get("preset") or "").strip()
            if active_name:
                try:
                    from services.word_automation import load_preset

                    loaded = load_preset(active_name)
                    active = loaded.model_dump(mode="json")
                    active["id"] = loaded.name
                    _studio_set_preset(session_id, active)
                except Exception:  # noqa: BLE001 - пресет мог быть переименован
                    active = None
        if not active:
            return (
                "Активный пресет не задан. Отправьте пресет из правой панели "
                "(кнопка «Отправить пресет в модель») или прикрепите документ."
            )
        return "Активный пресет оформления (JSON):\n" + json.dumps(active, ensure_ascii=False)

    if fn_name == "get_preset":
        preset_name = str(fn_args.get("preset_name") or "").strip()
        if not preset_name:
            return "Укажите preset_name (имя сохранённого пресета)."
        from services.word_automation.preset_schema import list_presets, preset_path

        # Проверяем наличие файла заранее: load_preset мог бы пересоздать
        # удалённый базовый пресет — здесь это нежелательно.
        if not preset_path(preset_name).exists():
            available = ", ".join(list_presets()) or "нет"
            return f"Пресет '{preset_name}' не найден. Доступные пресеты: {available}."
        from services.word_automation import load_preset

        try:
            loaded = load_preset(preset_name)
        except Exception as exc:  # noqa: BLE001 - внятное сообщение модели
            return f"Пресет '{preset_name}' не найден: {exc}"
        data = loaded.model_dump(mode="json")
        data["id"] = loaded.name
        _studio_set_preset(session_id, data)
        return f"Пресет '{loaded.name}' (JSON):\n" + json.dumps(data, ensure_ascii=False)

    path = str(state.get("path") or "")
    if not path or not os.path.isfile(path):
        return (
            "Документ ещё не прикреплён к студии. Попросите пользователя добавить "
            ".docx в Document Studio — после этого он станет доступен инструментам."
        )

    def _summarize(prefix: str, stats: dict) -> str:
        return (
            f"{prefix} Готово. Изменено: абзацев {stats.get('paragraphs_touched', 0)}, "
            f"таблиц {stats.get('tables_touched', 0)}, "
            f"рисунков {stats.get('figures_touched', 0)}, "
            f"секций {stats.get('sections_touched', 0)}."
        )

    try:
        if fn_name == "get_document_state":
            preview = preview_document(path)
            page = preview.get("page") or {}
            tables = sum(1 for b in preview.get("blocks", []) if b.get("type") == "table")
            images = sum(1 for b in preview.get("blocks", []) if b.get("type") == "image")
            return (
                f"Документ «{state.get('name') or os.path.basename(path)}»: поля "
                f"{page.get('top')}/{page.get('bottom')}/{page.get('left')}/"
                f"{page.get('right')} мм, блоков {len(preview.get('blocks', []))}, "
                f"таблиц {tables}, рисунков {images}."
            )

        if fn_name == "apply_document_preset":
            preset_name = str(fn_args.get("preset_name") or "").strip()
            if not preset_name:
                return "Не указано имя пресета для применения."
            outcome = apply_preset(path, preset_name)
            state["preset"] = preset_name
            try:
                from services.word_automation import load_preset

                loaded = load_preset(preset_name)
                stored = loaded.model_dump(mode="json")
                stored["id"] = loaded.name
                _studio_set_preset(session_id, stored)
            except Exception:  # noqa: BLE001 - активный пресет останется прежним
                pass
            return _summarize(
                f"Применён пресет «{preset_name}».", outcome.get("stats") or {}
            )

        # Остальные инструменты собираются в список правил.
        rules: list[dict] = []
        if fn_name == "set_page_margins":
            for key in ("top", "bottom", "left", "right"):
                if fn_args.get(key) is not None:
                    rules.append({"scope": "page", "property": key, "value": fn_args[key]})
        elif fn_name == "set_paragraph_format":
            scope = str(fn_args.get("scope") or "body")
            for key in (
                "space_before", "space_after", "line_spacing",
                "line_spacing_rule", "first_line_indent", "alignment",
            ):
                if fn_args.get(key) is not None:
                    rules.append({"scope": scope, "property": key, "value": fn_args[key]})
        elif fn_name == "set_font":
            scope = str(fn_args.get("scope") or "body")
            for key in ("font_name", "font_size", "bold", "italic"):
                if fn_args.get(key) is not None:
                    rules.append({"scope": scope, "property": key, "value": fn_args[key]})
        elif fn_name == "format_tables":
            mapping = {
                "table_alignment": "table_alignment",
                "cell_alignment": "alignment",
                "font_name": "font_name",
                "font_size": "font_size",
                "cell_margin_top": "cell_margin_top",
                "cell_margin_bottom": "cell_margin_bottom",
                "cell_margin_left": "cell_margin_left",
                "cell_margin_right": "cell_margin_right",
            }
            for arg_key, prop in mapping.items():
                if fn_args.get(arg_key) is not None:
                    rules.append({"scope": "tables", "property": prop, "value": fn_args[arg_key]})
        elif fn_name == "align_figures":
            if fn_args.get("alignment") is not None:
                rules.append(
                    {"scope": "figures", "property": "alignment", "value": fn_args["alignment"]}
                )
        elif fn_name == "reset_spacing":
            scope = str(fn_args.get("scope") or "all")
            rules.extend(
                [
                    {"scope": scope, "property": "space_before", "value": 0},
                    {"scope": scope, "property": "space_after", "value": 0},
                    {"scope": scope, "property": "line_spacing", "value": 1.0},
                    {"scope": scope, "property": "line_spacing_rule", "value": "single"},
                ]
            )
        elif fn_name == "apply_document_rules":
            raw = fn_args.get("rules")
            if isinstance(raw, list):
                rules.extend(raw)
        else:
            return f"Инструмент '{fn_name}' не поддерживается."

        if not rules:
            return "Не передано ни одного значения для изменения — уточните параметры."

        outcome = apply_rules(path, rules, output_path=path)
        return _summarize(f"Правил применено: {len(rules)}.", outcome.get("stats") or {})
    except PresetError as exc:
        return f"Не удалось применить правило: {exc}"
    except Exception as exc:  # noqa: BLE001 - инструмент не должен ронять чат
        return f"Ошибка инструмента '{fn_name}': {exc}"


def _word_save_preset_payload(req_data: dict) -> dict:
    """Сохраняет присланный пресет в ``presets/`` под заданным именем.

    Пресет проходит валидацию схемы (:func:`validate_preset_payload`), которая
    теперь поддерживает блоки ``title_page``, ``special_sections`` и
    ``page_numbering``; предупреждения валидатора возвращаются фронтенду.
    """
    from services.word_automation import (
        load_preset,
        save_preset,
        validate_preset_payload,
    )

    data = req_data.get("preset")
    if not isinstance(data, dict):
        raise ValueError("Передайте preset (объект пресета)")
    report = validate_preset_payload(data)
    if not report.get("ok"):
        problems = "; ".join(
            err.get("message", "")
            if isinstance(err, dict)
            else str(err)
            for err in report.get("errors", [])
        )
        raise ValueError(f"Пресет не прошёл валидацию: {problems}")
    preset = load_preset(data)
    name = str(req_data.get("name") or "").strip() or None
    path = save_preset(preset, name)
    # save_preset мог переименовать пресет — читаем финальную версию с диска.
    final = load_preset(path)
    payload = final.model_dump(mode="json")
    payload["id"] = final.name
    log_event(f"[*] Пресет сохранён: {path}")
    return {
        "ok": True,
        "path": path,
        "preset": payload,
        "warnings": report.get("warnings", []),
        "sections": report.get("sections", []),
    }


def _api_safe_msg(m: dict) -> dict:
    """Оставляет только ключи, легальные для chat-completions API.

    В истории хранятся служебные поля (display — текст для отображения,
    attachments — чипсы файлов); они не должны утекать в JSON-запрос к модели.
    """
    out = {"role": m.get("role"), "content": m.get("content", "")}
    if m.get("tool_calls"):
        out["tool_calls"] = m["tool_calls"]
    if m.get("tool_call_id"):
        out["tool_call_id"] = m["tool_call_id"]
    return out


def _repair_tool_chain(msgs: list) -> list:
    """Чинит начало окна истории под требования OpenAI-совместимого протокола.

    Цикл (а не одиночный if): после удаления битого assistant с tool_calls
    следом могут остаться его tool-ответы — их тоже нужно снять, иначе запрос
    начинается с role="tool" (400 у строгих провайдеров, зацикливание у прокси).
    """
    changed = True
    while msgs and changed:
        changed = False
        head = msgs[0] if isinstance(msgs[0], dict) else {}
        if head.get("role") not in ("user", "assistant"):
            msgs.pop(0)
            changed = True
            continue
        if head.get("role") == "assistant" and head.get("tool_calls"):
            _need = {
                tc.get("id")
                for tc in head["tool_calls"]
                if isinstance(tc, dict)
            }
            _have = {
                m.get("tool_call_id")
                for m in msgs[1:]
                if isinstance(m, dict) and m.get("role") == "tool"
            }
            if _need and not _need.issubset(_have):
                msgs.pop(0)
                while msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "tool":
                    msgs.pop(0)
                changed = True
    return msgs


def _prepare_messages_for_api(
    messages: list, max_chars: int | None = 24000, max_msgs: int | None = None
) -> list:
    """Подготавливает историю перед отправкой в LLM.

    Для обычного режима max_chars=None отключает клиентское ограничение размера
    истории и содержимого сообщений. Ограничения контекста в этом режиме
    остаются ответственностью API-провайдера.

    max_msgs — лимит количества сообщений (system не считается): растущая
    история не должна раздувать каждый запрос к модели (латентность и цена).
    Управляется config.json → max_history_messages (0 = без лимита).

    Текущий ход — от последнего user-сообщения до конца массива — не режется:
    в цикле tool_calls он растёт (assistant+tool на каждый шаг), и оконный
    срез иначе выталкивает исходный вопрос пользователя из окна — модель
    остаётся без вопроса и отвечает базовым приветствием на голый системный
    промпт (проявлялось после ~10 циклов поиска при max_history_messages=20).
    Лимит применяется только к сообщениям ПРЕДЫДУЩИХ ходов.
    """
    if not messages:
        return []
    system_msg = None
    other_msgs = messages
    if messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        other_msgs = messages[1:]

    if max_msgs:
        # Индекс последнего user-сообщения — граница текущего хода.
        last_user_idx = None
        for i in range(len(other_msgs) - 1, -1, -1):
            if (other_msgs[i] or {}).get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            if len(other_msgs) > max_msgs:
                other_msgs = other_msgs[-max_msgs:]
        else:
            current_turn = other_msgs[last_user_idx:]
            prior = other_msgs[:last_user_idx]
            prior_budget = max(0, max_msgs - len(current_turn))
            if len(prior) > prior_budget:
                prior = prior[-prior_budget:] if prior_budget else []
            other_msgs = prior + current_turn
    # Целостность протокола — ВНЕ зависимости от лимита: раньше блок жил внутри
    # `if max_msgs:`, и при max_history_messages=0 (без лимита) валидации истории
    # не было вовсе, а на диске tool-цепочки сохраняются как есть.
    other_msgs = _repair_tool_chain(other_msgs)

    def _msg_len(m):
        try:
            return len(json.dumps(m.get("content", ""), ensure_ascii=False, default=str))
        except Exception:
            return len(str(m.get("content", "")))

    # Находим самое свежее user-сообщение с картинками — только ему оставляем images
    last_img_idx = -1
    for i in range(len(other_msgs) - 1, -1, -1):
        c = (other_msgs[i] or {}).get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            last_img_idx = i
            break

    # Лимиты на отдельное сообщение задаются в профиле (0 = без лимита) и
    # приходят через окружение. Раньше здесь были жёсткие 8000/12000 символов,
    # из-за чего содержимое вложений и сам запрос пользователя обрезались.
    def _env_lim(name):
        try:
            v = int(os.environ.get(name, "0") or "0")
            return v if v > 0 else 0
        except (TypeError, ValueError):
            return 0

    msg_max_chars = _env_lim("CHAT_MSG_MAX_CHARS")
    text_part_max_chars = _env_lim("CHAT_MSG_TEXT_PART_MAX_CHARS")

    cleaned = []
    for i, m in enumerate(other_msgs):
        if not isinstance(m, dict):
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            parts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "image_url":
                    if i == last_img_idx and len(parts) < 4:
                        parts.append(p)
                    else:
                        parts.append({"type": "text", "text": "[изображение из истории опущено для экономии токенов]"})
                elif p.get("type") == "text":
                    t = str(p.get("text", ""))
                    if text_part_max_chars and len(t) > text_part_max_chars:
                        t = t[:text_part_max_chars] + "\n...(обрезано для экономии квоты)..."
                    parts.append({"type": "text", "text": t})
                else:
                    parts.append(p)
            m = dict(m)
            m["content"] = parts
        elif isinstance(c, str):
            if msg_max_chars and len(c) > msg_max_chars:
                m = dict(m)
                m["content"] = c[:msg_max_chars] + "\n...(обрезано для экономии квоты)..."
        # Служебные ключи истории (display, attachments) не должны попадать
        # в запрос к API — оставляем только легальный набор полей.
        cleaned.append(_api_safe_msg(m))
    # Лимит количества сообщений не задан (max_msgs/max_chars = None) — отдаём
    # всю историю, как есть.
    if max_chars is None:
        return ([_api_safe_msg(system_msg)] if system_msg else []) + cleaned

    # Берём только последние ~30 сообщений — старые всё равно не влезут в лимит
    cleaned = cleaned[-30:]

    selected = []
    current_chars = _msg_len(system_msg) if system_msg else 0

    for m in reversed(cleaned):
        m_len = _msg_len(m)
        if current_chars + m_len > max_chars and selected:
            break
        if m_len > max_chars:
            m = dict(m)
            if isinstance(m.get("content"), str):
                m["content"] = m["content"][:max_chars] + "\n...(содержимое обрезано для экономии квоты)..."
            m_len = max_chars
        selected.append(m)
        current_chars += m_len

    selected.reverse()
    if system_msg:
        return [_api_safe_msg(system_msg)] + selected
    return selected
HISTORY_FILE = str(USER_DATA_DIR / "sessions_history.json")
NOTES_FILE = str(USER_DATA_DIR / "notes.json")
CONFIG_PATH = str(USER_DATA_DIR / "config.json")
FRONTEND_DIR = os.path.join(BASE_DIR, "UI")
HOST = "127.0.0.1"
PORT = 3000
BRIDGE_PORT = 5000


def load_history():
    """Загрузить все чаты из отдельных файлов в history/ для совместимости API."""
    try:
        config = load_config()
        session_ids = set(config.get("sessions_meta", {}).keys())
        history_dir = os.path.join(os.path.dirname(str(__import__("config_manager").CONFIG_PATH)), "history")
        if os.path.isdir(history_dir):
            session_ids.update(
                os.path.splitext(name)[0]
                for name in os.listdir(history_dir)
                if name.endswith(".json") and not name.endswith(".tmp")
            )
        return {session_id: load_chat_history(session_id) for session_id in session_ids}
    except Exception as e:
        log_event(f"Ошибка чтения истории: {e}")
        return {}


def save_history(history_data):
    """Сохранить каждый чат отдельным JSON-файлом, не помещая историю в config.json."""
    try:
        if not isinstance(history_data, dict):
            history_data = {}
        for session_id, messages in history_data.items():
            save_chat_history(session_id, messages)
    except Exception as e:
        log_event(f"Ошибка сохранения истории: {e}")


def load_notes():
    if os.path.exists(NOTES_FILE):
        try:
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            log_event(f"[!] Ошибка чтения notes.json: {e}")
    return []


def save_notes(notes):
    try:
        temporary_file = NOTES_FILE + ".tmp"
        with open(temporary_file, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
        os.replace(temporary_file, NOTES_FILE)
    except Exception as e:
        log_event(f"[!] Ошибка записи notes.json: {e}")


sessions_memory = load_history()
sessions_meta_memory = {}

SESSIONS_META_FILE = str(USER_DATA_DIR / "sessions_meta.json")


def load_sessions_meta():
    try:
        config = load_config()
        metadata = config.get("sessions_meta", {})
        return metadata if isinstance(metadata, dict) else {}
    except Exception as e:
        log_event(f"[!] Ошибка чтения метаданных чатов: {e}")
        return {}


def save_sessions_meta(meta):
    try:
        config = load_config()
        config["sessions_meta"] = meta if isinstance(meta, dict) else {}
        save_config(config)
    except Exception as e:
        log_event(f"[!] Ошибка записи метаданных чатов: {e}")


# --------------------------------------------------------------------------------------
# Document Studio: отдельные профили и чаты (полностью независимы от основных)
# --------------------------------------------------------------------------------------

STUDIO_ACTIVE_KEY = "studio_active_profile"
STUDIO_META_KEY = "studio_sessions_meta"
#: Флаг профиля, принадлежащего студии (хранится в общем config["profiles"]).
STUDIO_PROFILE_FLAG = "is_studio"
#: Имя стартового профиля студии.
STUDIO_DEFAULT_PROFILE = "studio"
#: Ключ рабочего документа студии (общий для всех её чатов; совпадает с фронтендом).
STUDIO_SESSION_ID = "document-studio"

#: In-memory кэш чатов студии и их метаданных (не смешивается с основными чатами).
studio_sessions_memory: dict = {}
studio_sessions_meta_memory: dict = {}
#: Чаты студии, удалённые в этом запуске (блокирует «воскрешение»).
DELETED_STUDIO_SESSIONS: set[str] = set()


def load_studio_sessions_meta() -> dict:
    """Метаданные чатов студии (id -> {personaId, title, timestamp})."""
    try:
        metadata = load_config().get(STUDIO_META_KEY, {})
        return metadata if isinstance(metadata, dict) else {}
    except Exception as e:
        log_event(f"[!] Ошибка чтения метаданных чатов студии: {e}")
        return {}


def save_studio_sessions_meta(meta: dict) -> None:
    """Сохраняет метаданные чатов студии в config.json."""
    try:
        config = load_config()
        config[STUDIO_META_KEY] = meta if isinstance(meta, dict) else {}
        save_config(config)
    except Exception as e:
        log_event(f"[!] Ошибка записи метаданных чатов студии: {e}")


def _is_studio_profile(data: object) -> bool:
    """Профиль принадлежит студии (флаг is_studio в общем config["profiles"])."""
    return isinstance(data, dict) and bool(data.get(STUDIO_PROFILE_FLAG))


def _make_studio_profile(base: dict | None = None) -> dict:
    """Стартовый профиль студии: копия базового профиля + пометка студии."""
    profile = dict(base or {})
    profile[STUDIO_PROFILE_FLAG] = True
    profile["display_name"] = str(profile.get("display_name") or "").strip() or "Документ-студия"
    profile.setdefault("emoji", "📄")
    ds = load_config().get("document_studio")
    if isinstance(ds, dict):
        if str(ds.get("base_url") or "").strip():
            profile["base_url"] = str(ds["base_url"]).strip()
        if str(ds.get("api_key") or "").strip():
            profile["api_key"] = str(ds["api_key"]).strip()
        if str(ds.get("model") or "").strip():
            profile["model"] = str(ds["model"]).strip()
        if str(ds.get("system_prompt") or "").strip():
            profile["system_prompt"] = str(ds["system_prompt"])
        if isinstance(ds.get("temperature"), (int, float)):
            profile["temperature"] = float(ds["temperature"])
        if isinstance(ds.get("request_timeout"), (int, float)) and ds["request_timeout"]:
            profile["request_timeout"] = int(ds["request_timeout"])
    return profile


def ensure_studio_profiles() -> None:
    """Гарантирует наличие хотя бы одного профиля студии (иначе создаёт стартовый)."""
    config = load_config()
    profiles = config.get("profiles") or {}
    studio = {k: v for k, v in profiles.items() if _is_studio_profile(v)}
    if studio:
        if config.get(STUDIO_ACTIVE_KEY) not in studio:
            config[STUDIO_ACTIVE_KEY] = next(iter(studio))
            save_config(config)
        return
    base = dict(profiles.get(str(config.get("active_profile", "")) or "") or {})
    profiles[STUDIO_DEFAULT_PROFILE] = _make_studio_profile(base)
    config["profiles"] = profiles
    config[STUDIO_ACTIVE_KEY] = STUDIO_DEFAULT_PROFILE
    save_config(config)
    log_event("[STUDIO] создан стартовый профиль студии 'studio'")


def _studio_profiles() -> dict:
    """Только профили студии из общего config["profiles"]."""
    profiles = load_config().get("profiles") or {}
    return {k: v for k, v in profiles.items() if _is_studio_profile(v)}


def _studio_active_name() -> str:
    return str(load_config().get(STUDIO_ACTIVE_KEY) or "").strip()


def _studio_persona(p_id: str, data: dict, active: str = "") -> dict:
    """Профиль студии в форме фронтенда (как персона основного чата)."""
    return {
        "id": p_id,
        "name": str(data.get("display_name") or "").strip() or p_id.upper(),
        "emoji": str(data.get("emoji") or "📄"),
        "description": str(data.get("description") or ""),
        "modelName": str(data.get("model", "")),
        "systemPrompt": str(data.get("system_prompt", "")),
        "temperature": float(data.get("temperature", 0.7)),
        "baseUrl": str(data.get("base_url") or ""),
        "apiKey": "",
        "hasApiKey": bool(data.get("api_key") or data.get("api_key_env")),
        "requestTimeout": int(data.get("request_timeout", 120) or 120),
        "accentColor": "#6366f1" if p_id == active else "#64748b",
    }


def _studio_init_payload() -> dict:
    """Профили студии + список её чатов (отдельно от основного чата)."""
    ensure_studio_profiles()
    config = load_config()
    profiles = _studio_profiles()
    active = str(config.get(STUDIO_ACTIVE_KEY) or "")
    personas = [_studio_persona(pid, data, active) for pid, data in profiles.items()]
    meta = load_studio_sessions_meta()
    ids = set(meta.keys())
    try:
        from config_manager import list_studio_history_ids

        ids.update(list_studio_history_ids())
    except Exception:  # noqa: BLE001 - отсутствие каталога не критично
        pass
    sessions = []
    for sid in ids:
        if sid in DELETED_STUDIO_SESSIONS:
            continue
        info = meta.get(sid) or {}
        sessions.append(
            {
                "id": sid,
                "title": str(info.get("title") or "Новый чат"),
                "personaId": str(info.get("personaId") or active),
                "timestamp": str(info.get("timestamp") or ""),
            }
        )
    sessions.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return {
        "profiles": personas,
        "active_profile": active,
        "sessions": sessions,
        "last_active_session": str(config.get("studio_last_session") or ""),
    }


def _studio_profile_from_persona(persona: dict, existing: dict | None = None) -> dict:
    """Преобразует профиль студии из формы фронтенда в запись config.json."""
    return {
        STUDIO_PROFILE_FLAG: True,
        "display_name": str(persona.get("name") or "").strip(),
        "emoji": str(persona.get("emoji") or "📄"),
        "description": str(persona.get("description") or ""),
        "base_url": str(persona.get("baseUrl") or ""),
        "model": str(persona.get("modelName") or ""),
        "api_key": retained_secret(persona.get("apiKey"), (existing or {}).get("api_key"), persona.get("clearApiKey")),
        "system_prompt": str(persona.get("systemPrompt") or ""),
        "temperature": float(persona.get("temperature", 0.7) or 0.7),
        "request_timeout": int(persona.get("requestTimeout", 120) or 120),
    }


def _studio_handle(path: str, req_data: dict) -> dict:
    """Обрабатывает действия студии: профили и чаты (отдельное хранилище)."""
    if path == "/api/studio/save_profile":
        persona = req_data.get("persona")
        if not isinstance(persona, dict) or not str(persona.get("id") or "").strip():
            raise ValueError("persona.id required")
        ensure_studio_profiles()
        config = load_config()
        profiles = config.get("profiles") or {}
        pid = str(persona["id"]).strip()
        existing = profiles.get(pid, {})
        if existing and not _is_studio_profile(existing):
            raise ValueError("Cannot overwrite a main profile from Document Studio")
        profiles[pid] = _studio_profile_from_persona(persona, existing)
        config["profiles"] = profiles
        if config.get(STUDIO_ACTIVE_KEY) not in _studio_profiles():
            config[STUDIO_ACTIVE_KEY] = pid
        save_config(config)
        return {"ok": True}

    if path == "/api/studio/delete_profile":
        pid = str(req_data.get("profile_id") or "").strip()
        config = load_config()
        profiles = config.get("profiles") or {}
        if _is_studio_profile(profiles.get(pid)):
            del profiles[pid]
        config["profiles"] = profiles
        save_config(config)
        ensure_studio_profiles()
        return {"ok": True, "active_profile": _studio_active_name()}

    if path == "/api/studio/set_active_profile":
        pid = str(req_data.get("profile_id") or "").strip()
        config = load_config()
        profiles = config.get("profiles") or {}
        if _is_studio_profile(profiles.get(pid)):
            config[STUDIO_ACTIVE_KEY] = pid
            save_config(config)
        return {"ok": True, "active_profile": pid}

    if path == "/api/studio/session/upsert":
        sid = str(req_data.get("id") or req_data.get("session_id") or "").strip()
        if not sid:
            raise ValueError("id required")
        DELETED_STUDIO_SESSIONS.discard(sid)
        meta = load_studio_sessions_meta()
        cur = meta.get(sid) or {}
        pid = str(
            req_data.get("personaId")
            or req_data.get("persona_id")
            or cur.get("personaId")
            or _studio_active_name()
            or "studio"
        )
        title = (req_data.get("title") or cur.get("title") or "Новый чат").strip() or "Новый чат"
        meta[sid] = {
            "personaId": pid,
            "title": title,
            "timestamp": req_data.get("timestamp")
            or cur.get("timestamp")
            or datetime.now().strftime("%d.%m %H:%M"),
        }
        studio_sessions_meta_memory[sid] = meta[sid]
        save_studio_sessions_meta(meta)
        if sid not in studio_sessions_memory:
            studio_sessions_memory[sid] = load_studio_history(sid)
        return {"ok": True, "session": {"id": sid, **meta[sid]}}

    if path == "/api/studio/session/delete":
        sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
        if not sid:
            raise ValueError("session_id required")
        DELETED_STUDIO_SESSIONS.add(sid)
        studio_sessions_memory.pop(sid, None)
        studio_sessions_meta_memory.pop(sid, None)
        for item in purge_studio_session_storage(sid):
            log_event(f"[STUDIO DELETE] Стёрто: {item}")
        meta = load_studio_sessions_meta()
        if sid in meta:
            del meta[sid]
            save_studio_sessions_meta(meta)
        return {"ok": True, "session_id": sid}

    if path == "/api/studio/session/clear":
        sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
        if not sid:
            raise ValueError("session_id required")
        messages = studio_sessions_memory.get(sid)
        if messages is None:
            messages = load_studio_history(sid)
        kept = (
            messages[:1]
            if messages
            and isinstance(messages[0], dict)
            and messages[0].get("role") == "system"
            else []
        )
        studio_sessions_memory[sid] = kept
        save_studio_history(sid, kept)
        return {"ok": True}

    if path == "/api/studio/session/messages":
        sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
        if not sid:
            raise ValueError("session_id required")
        messages = studio_sessions_memory.get(sid)
        if messages is None:
            messages = load_studio_history(sid)
        return {"messages": messages}

    raise ValueError(f"Неизвестный запрос студии: {path}")


def _meta_title_from_prompt(prompt, limit=48):
    try:
        text = str(prompt or "").strip().replace("\n", " ")
        while "  " in text:
            text = text.replace("  ", " ")
        # срезаем служебный префикс про файлы
        if text.startswith("[Содержимое файла:"):
            # берём последнюю непустую строку как реальный вопрос
            lines = [l.strip() for l in str(prompt or "").splitlines() if l.strip()]
            text = lines[-1] if lines else text
        if len(text) > limit:
            return text[:limit].rstrip() + "…"
        return text or "Новый чат"
    except Exception:
        return "Новый чат"


def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" and p.get("text"):
                    parts.append(str(p["text"]))
                elif p.get("type") == "image_url":
                    parts.append("[изображение]")
        return "\n".join(parts)
    return str(content or "")

try:
    sessions_meta_memory = load_sessions_meta()
except Exception:
    sessions_meta_memory = {}

# id чатов, удалённых в текущем запуске. Набор не даёт «воскреснуть» стёртой
# сессии при сохранении других данных, позднем /api/chat или повторном /api/init.
DELETED_SESSIONS: set[str] = set()



def get_desktop_dir():
    user_home = os.path.expanduser("~")
    for name in ["Desktop", "Рабочий стол"]:
        p = os.path.join(user_home, name)
        if os.path.exists(p):
            return p
    return user_home


def _pick_directory_native(initialdir: str) -> str:
    """Открывает системный диалог выбора папки в отдельном процессе.

    Диалог показывается в дочернем процессе на его ГЛАВНОМ потоке — это изолирует
    tkinter от потоков HTTP-сервера и от уже запущенного GUI родителя (pywebview/
    WebView2/CLR), где диалог в фоновом потоке не выходил на передний план либо
    ронял запрос. Результат передаётся через временный файл (pythonw без stdout).
    """
    import subprocess
    import tempfile

    script = (
        "import sys\n"
        "from tkinter import Tk, filedialog\n"
        "root = Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        "root.update()\n"
        "d = filedialog.askdirectory(title='Выберите папку для скачивания', initialdir=sys.argv[1])\n"
        "root.destroy()\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write(d or '')\n"
    )
    fd, result_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        subprocess.run(
            [sys.executable, "-c", script, initialdir or "", result_path],
            timeout=300,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(result_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass


def _unique_target_path(directory: str, filename: str) -> str:
    """Несуществующий путь для сохранения файла в directory.

    При конфликте имени добавляет « (1)», « (2)», … перед расширением, чтобы
    авто-скачивание не затирало уже существующие файлы.
    """
    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    for idx in range(1, 1000):
        candidate = os.path.join(directory, f"{stem} ({idx}){ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(directory, f"{stem}_{int(time.time())}{ext}")


# Последний успешно полученный баланс: при сбое сети/API показываем его,
# а не обнуляем витрину (иначе баланс «слетал» в 0 при каждом таймауте).
_LAST_KNOWN_BALANCE: dict = {"value": None}

# TTL-кэш баланса: он запрашивается на каждом /api/chat И каждом /api/init
# (несколько раз за одно действие пользователя). Без кэша каждый флап
# канала добавлял 6-12 с к ответу/обновлению интерфейса.
_BALANCE_CACHE: dict = {"value": None, "ts": 0.0, "key": ""}
_BALANCE_TTL = 45.0

# Фоновое обновление баланса (stale-while-revalidate). Просроченный кэш больше
# не блокирует ответ: отдаём последнее известное значение и обновляем его в
# отдельном потоке. Раньше просрочка означала синхронный GET /users/balance
# (до двух попыток по 6 с) прямо в критическом пути /api/chat — пользователь
# ждал баланс, хотя текст ответа был давно готов.
_BALANCE_REFRESH_LOCK = threading.Lock()
_BALANCE_REFRESH_STATE: dict = {"running": False, "ts": 0.0}
_BALANCE_REFRESH_MIN_INTERVAL = 5.0


def get_live_balance(api_key, base_url="", force=False):
    if not api_key:
        return 0.0

    # Принудительное обновление (кнопка «Refresh balance» в шапке): игнорируем
    # TTL-кэш и синхронно идём в SprutDock; при сбое отдаём последнее известное.
    if force:
        balance = _fetch_balance(api_key)
        if balance is not None:
            return balance
        return _LAST_KNOWN_BALANCE.get("value") or 0.0

    now = time.time()
    if (
        _BALANCE_CACHE["key"] == api_key
        and _BALANCE_CACHE["value"] is not None
        and now - _BALANCE_CACHE["ts"] < _BALANCE_TTL
    ):
        return _BALANCE_CACHE["value"]

    # Просроченный кэш: отдаём последнее известное значение сразу, а обновление
    # запускаем фоном — ответ пользователю больше не ждёт сеть.
    stale = None
    if _BALANCE_CACHE["key"] == api_key and _BALANCE_CACHE["value"] is not None:
        stale = _BALANCE_CACHE["value"]
    if stale is None:
        stale = _LAST_KNOWN_BALANCE.get("value")
    if stale is not None:
        _spawn_balance_refresh(api_key)
        return stale

    # Кэша нет вовсе (первый запрос после старта) — без синхронного обращения
    # не обойтись, иначе в интерфейсе будет «0.00 ₽» до следующего сообщения.
    balance = _fetch_balance(api_key)
    return balance if balance is not None else 0.0


def _spawn_balance_refresh(api_key) -> bool:
    """Запускает фоновое обновление баланса. True = обновление обеспечено.

    Защита от «стаи» потоков: не более одного одновременного запроса и не чаще
    одного раза в _BALANCE_REFRESH_MIN_INTERVAL секунд.
    """
    with _BALANCE_REFRESH_LOCK:
        now = time.time()
        if _BALANCE_REFRESH_STATE["running"]:
            return True
        if now - _BALANCE_REFRESH_STATE["ts"] < _BALANCE_REFRESH_MIN_INTERVAL:
            return False
        _BALANCE_REFRESH_STATE["running"] = True
        _BALANCE_REFRESH_STATE["ts"] = now

    def _worker():
        try:
            _fetch_balance(api_key)
        finally:
            with _BALANCE_REFRESH_LOCK:
                _BALANCE_REFRESH_STATE["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return True


def _fetch_balance(api_key):
    """Синхронный GET баланса SprutDock. Возвращает float либо None при неудаче."""
    # Официальный эндпоинт SprutDock по документации: GET /v1/users/balance
    url = "https://sprutdock.ru/v1/users/balance"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    # Канал к API может отказывать по-разному: пробуем сначала как настроено
    # в системе (прокси), затем напрямую — ретраи спасают баланс от «0» при
    # сбоях TLS-рукопожатия у провайдера.
    last_error = None
    for attempt in range(2):
        try:
            if attempt == 0:
                resp = requests.get(url, headers=headers, timeout=6)
            else:
                resp = requests.get(
                    url, headers=headers, timeout=6, proxies={"http": None, "https": None}
                )
            if resp.status_code == 200:
                data = resp.json()
                # Ответ SprutDock содержит точный баланс в рублях
                val = data.get("balance", data.get("data", {}).get("balance", 0.0))
                balance = float(val)
                _LAST_KNOWN_BALANCE["value"] = balance
                _BALANCE_CACHE.update({"value": balance, "ts": time.time(), "key": api_key})
                return balance
            last_error = f"HTTP {resp.status_code}"
        except Exception as e:
            last_error = e
    log_event(f"[!] Ошибка получения баланса SprutDock: {last_error}")
    return None


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _authorized(self):
        if authorized(self.headers, BRIDGE_PORT):
            return True
        self._set_cors(403)
        self.wfile.write(b'{"error":"Forbidden"}')
        self.close_connection = True
        return False

    def _set_cors(self, status=200):
        self._headers_sent = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Клиент мог закрыть вкладку или отменить запрос после получения ответа.
            # Это штатное завершение соединения, а не ошибка обработки чата.
            return

    def do_OPTIONS(self):
        self._set_cors(403)

    def do_GET(self):
        if not self._authorized():
            return
        if self.path.startswith("/api/download-local"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            requested_path = relocated_user_file(params.get("path", [""])[0])
            requested_name = os.path.basename(params.get("name", ["attachment"])[0]) or "attachment"
            if not FILE_ACCESS.allowed(requested_path):
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
                return
            try:
                file_size = os.path.getsize(requested_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Content-Disposition", _content_disposition(requested_name))
                self.end_headers()
                with open(requested_path, "rb") as download_file:
                    shutil.copyfileobj(download_file, self.wfile)
            except OSError:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл недоступен"}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path.startswith("/api/download/"):
            file_id = self.path.split("/api/download/", 1)[1].split("?", 1)[0]
            upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
            matches = [name for name in os.listdir(upload_dir)] if os.path.isdir(upload_dir) else []
            matched = next((name for name in matches if name.startswith(file_id + "_")), None)
            if not matched:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
                return
            stored_path = os.path.join(upload_dir, matched)
            try:
                file_size = os.path.getsize(stored_path)
                safe_name = matched.split("_", 1)[1]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Content-Disposition", _content_disposition(safe_name))
                self.end_headers()
                with open(stored_path, "rb") as download_file:
                    shutil.copyfileobj(download_file, self.wfile)
            except OSError:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл недоступен"}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path.startswith("/api/generated/") and self.path.endswith("/preview"):
            file_id = self.path.split("/api/generated/", 1)[1].split("/", 1)[0]
            preview = file_tools.preview_generated_file(file_id)
            if not preview:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
                return
            self._set_cors()
            self.wfile.write(json.dumps({"ok": True, **preview}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path.startswith("/api/generated/"):
            file_id = self.path.split("/api/generated/", 1)[1].split("?", 1)[0]
            gen_path, gen_name = _generated_file(file_id)
            if not gen_path:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
                return
            try:
                file_size = os.path.getsize(gen_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Content-Disposition", _content_disposition(gen_name or "file"))
                self.end_headers()
                with open(gen_path, "rb") as generated_file:
                    shutil.copyfileobj(generated_file, self.wfile)
            except OSError:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл недоступен"}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/word/presets":
            self._set_cors()
            self.wfile.write(
                json.dumps(_word_presets_payload(), ensure_ascii=False).encode("utf-8")
            )
            return

        if self.path == "/api/document-studio-settings":
            cfg = load_config()
            settings = cfg.get("document_studio", {})
            if not isinstance(settings, dict):
                settings = {}
            self._set_cors()
            self.wfile.write(
                json.dumps(
                    {
                        "base_url": str(settings.get("base_url", "")),
                        "api_key": "",
                        "has_api_key": bool(settings.get("api_key")),
                        "model": str(settings.get("model", "")),
                        "system_prompt": str(settings.get("system_prompt", "")),
                        "temperature": settings.get("temperature"),
                        "request_timeout": settings.get("request_timeout") or 0,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            return

        if self.path == "/api/studio/init":
            self._set_cors()
            self.wfile.write(
                json.dumps(_studio_init_payload(), ensure_ascii=False).encode("utf-8")
            )
            return

        if self.path == "/api/init":
            cfg = load_config()
            profiles = cfg.get("profiles", {})
            # Основной чат видит только НЕ-студийные профили: профили студии
            # скрыты здесь и показываются лишь при открытой студии.
            main_profiles = {
                name: data for name, data in profiles.items() if not _is_studio_profile(data)
            }
            active_profile_key = cfg.get("active_profile", "main")
            if active_profile_key not in main_profiles:
                active_profile_key = next(iter(main_profiles), "main")

            personas = []
            for p_id, p_data in main_profiles.items():
                personas.append(
                    {
                        "id": p_id,
                        # Отображаемое имя/эмодзи/описание профиля хранятся в config.json
                        # (display_name/emoji/description) и переживают перезапуск;
                        # без сохранённого имени показываем, как раньше, id в верхнем регистре.
                        "name": (str(p_data.get("display_name") or "").strip() or p_id.upper()),
                        "emoji": str(p_data.get("emoji") or "🤖"),
                        "description": str(p_data.get("description") or ""),
                        "tagline": p_data.get("model", ""),
                        "modelName": p_data.get("model", ""),
                        "systemPrompt": p_data.get("system_prompt", ""),
                        "temperature": float(p_data.get("temperature", 0.7)),
                        "baseUrl": p_data.get("base_url")
                        or p_data.get("api_base", "https://sprutdock.ru/v1"),
                        "apiKey": "",
                        "hasApiKey": bool(p_data.get("api_key") or p_data.get("api_key_env")),
                        "thinkingBudget": int(p_data.get("thinking_budget", 0)),

                        "maxSteps": max(1, min(200, int(p_data.get("max_steps", 60) or 60))),
                        "attachMaxImages": max(1, int(p_data.get("attach_max_images", 4) or 4)),
                        "attachMaxTextChars": max(100, int(p_data.get("attach_max_text_chars", 60000) or 60000)),
                        "attachMaxTextPerFile": max(100, int(p_data.get("attach_max_text_per_file", 60000) or 60000)),
                        "attachMaxTotalText": max(100, int(p_data.get("attach_max_total_text", 180000) or 180000)),
                        "attachCsvMaxRows": max(1, int(p_data.get("attach_csv_max_rows", 400) or 400)),
                        "attachXlsxMaxRows": max(1, int(p_data.get("attach_xlsx_max_rows", 400) or 400)),
                        "chatMsgMaxChars": max(0, int(p_data.get("chat_msg_max_chars", 0) or 0)),
                        "chatMsgTextPartMaxChars": max(0, int(p_data.get("chat_msg_text_part_max_chars", 0) or 0)),
                        "accentColor": "#6366f1"
                        if p_id == active_profile_key
                        else "#64748b",
                    }
                )

            active_data = profiles.get(active_profile_key, {})
            current_balance = get_live_balance(
                active_data.get("api_key", ""), active_data.get("base_url", "")
            )

            history_data = load_history()
            # подтягиваем мету сессий (привязка чат -> профиль + заголовки)
            try:
                meta_data = load_sessions_meta()
            except Exception:
                meta_data = {}
            if not isinstance(meta_data, dict):
                meta_data = {}
            # каждая сессия с диска обязана иметь мету, иначе фронт её не покажет
            meta_dirty = False
            # Чаты, удалённые в этом запуске не должны воскресать в списке:
            # иначе поздний /api/init вернёт стёртую сессию обратно в UI.
            for _sid in [s for s in list(history_data.keys()) if s in DELETED_SESSIONS]:
                history_data.pop(_sid, None)
            for _sid in [s for s in list(meta_data.keys()) if s in DELETED_SESSIONS]:
                meta_data.pop(_sid, None)
                meta_dirty = True
            for sid in list(history_data.keys()):
                m = meta_data.get(sid)
                if not isinstance(m, dict) or not m.get("personaId"):
                    meta_data[sid] = {
                        "personaId": active_profile_key,
                        "title": "Основной диалог" if sid == "default" else ("Чат %s" % str(sid)[-6:]),
                        "timestamp": "Ранее",
                    }
                    meta_dirty = True
            # пустые чаты, созданные фронтом (есть мета, но сообщений ещё нет)
            for sid in list(meta_data.keys()):
                if sid not in history_data:
                    history_data[sid] = []
            if meta_dirty:
                try:
                    save_sessions_meta(meta_data)
                except Exception:
                    pass
            default_history = history_data.get("default", [])
            total_chars = sum(
                len(_msg_text(m.get("content", ""))) for s in history_data.values() for m in s if isinstance(m, dict)
            )
            sessions_list = []
            session_messages = {}
            for sid, msgs in history_data.items():
                m = meta_data.get(sid) or {}
                sessions_list.append(
                    {
                        "id": sid,
                        "personaId": m.get("personaId") or active_profile_key,
                        "title": m.get("title") or ("Основной диалог" if sid == "default" else sid),
                        "timestamp": m.get("timestamp") or "Ранее",
                    }
                )
                clean = []
                for msg in (msgs or []):
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    # display — сырой текст пользователя (без содержимого файлов):
                    # content хранит промпт с вложениями для API, показывать
                    # его в ленте целиком нельзя.
                    _disp = msg.get("display")
                    text = _msg_text(
                        _disp if isinstance(_disp, str) and _disp.strip() else msg.get("content", "")
                    )
                    if not text.strip():
                        continue
                    entry = {"role": role, "content": text}
                    if isinstance(msg.get("attachments"), list) and msg["attachments"]:
                        entry["attachments"] = msg["attachments"]
                    if role == "assistant" and isinstance(msg.get("reasoning"), str) and msg["reasoning"].strip():
                        entry["reasoning"] = msg["reasoning"]
                    clean.append(entry)
                # свежие сессии первыми, default — всегда в списке
                session_messages[sid] = clean[-200:]
            # сортировка: default первый, остальные как есть (фронт сам отсортирует по времени)
            sessions_list.sort(key=lambda s: (s["id"] != "default", s["id"]), reverse=False)

            self._set_cors()
            payload = {
                "active_profile": active_profile_key,
                "search_api_key": "",
                "has_search_api_key": bool(cfg.get("search_api_key")),
                # Последний открытый чат — восстанавливается фронтендом после рестарта.
                "active_session": str(cfg.get("last_active_session") or ""),
                "personas": personas,
                "balance": current_balance,
                "used_tokens": total_chars // 4,
                "history": default_history,
                "sessions": sessions_list,
                "session_messages": session_messages,
                "notes": load_notes(),
            }
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/models"):
            cfg = load_config()
            profiles = cfg.get("profiles", {})
            active_profile_key = cfg.get("active_profile", "main")
            active_data = profiles.get(active_profile_key, {})

            api_base = active_data.get("base_url") or active_data.get(
                "api_base", "https://sprutdock.ru/v1"
            )
            api_key = active_data.get("api_key", "")

            models_url = f"{api_base.rstrip('/')}/models"
            models_list = []
            try:
                req = urllib.request.Request(
                    models_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                    },
                )
                with _NO_PROXY_OPENER.open(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    for m in data.get("data", []):
                        m_id = m.get("id")
                        if m_id:
                            models_list.append(
                                {
                                    "id": m_id,
                                    "name": m_id,
                                    "owned_by": m.get("owned_by", "SprutDock"),
                                }
                            )
            except Exception as e:
                log_event(f"[!] Ошибка загрузки списка моделей ({models_url}): {e}")

            self._set_cors()
            self.wfile.write(
                json.dumps({"models": models_list}, ensure_ascii=False).encode("utf-8")
            )
        elif self.path == "/api/prompt-generator-settings":
            cfg = load_config()
            settings = cfg.get("prompt_generator", {})
            if not isinstance(settings, dict):
                settings = {}
            self._set_cors()
            self.wfile.write(
                json.dumps(
                    {
                        "base_url": str(settings.get("base_url", "")),
                        "api_key": "",
                        "has_api_key": bool(settings.get("api_key")),
                        "model": str(settings.get("model", "")),
                        "system_prompt": str(settings.get("system_prompt", "")),
                        "idea": str(settings.get("idea", "")),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        elif self.path == "/api/os-tools/pending":
            import win_tools

            self._set_cors()
            self.wfile.write(
                json.dumps({"pending": win_tools.list_pending()}, ensure_ascii=False).encode("utf-8")
            )
            return
        elif self.path == "/api/proxy-settings":
            # Настройки прокси (вкладка «Прокси» в настройках интерфейса).
            cfg = load_config()
            full = str(cfg.get("proxy_url", "") or "")
            server, username, password = full, "", ""
            if full:
                parsed = urllib.parse.urlparse(full)
                if parsed.username:
                    netloc = parsed.hostname or ""
                    if parsed.port:
                        netloc += f":{parsed.port}"
                    server = f"{parsed.scheme or 'http'}://{netloc}"
                    username = parsed.username
                    password = parsed.password or ""
            self._set_cors()
            self.wfile.write(
                json.dumps(
                    {
                        "mode": str(cfg.get("proxy_mode", "auto") or "auto"),
                        "url": server,
                        "username": username,
                        "password": "",
                        "has_password": bool(password or cfg.get("proxy_pass")),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        elif self.path == "/api/file-click-mode":
            cfg = load_config()
            mode = str(cfg.get("file_click_mode", "download") or "download")
            if mode not in ("download", "preview"):
                mode = "download"
            self._set_cors()
            self.wfile.write(
                json.dumps({"mode": mode}, ensure_ascii=False).encode("utf-8")
            )
        elif self.path == "/api/tool-send-mode":
            cfg = load_config()
            mode = str(cfg.get("tool_send_mode", "gated") or "gated").strip().lower()
            if mode not in ("gated", "all", "embedding"):
                mode = "gated"
            self._set_cors()
            self.wfile.write(
                json.dumps({"mode": mode}, ensure_ascii=False).encode("utf-8")
            )
        elif self.path == "/api/download-base-path":
            cfg = load_config()
            base = str(cfg.get("download_base_path", "") or "").strip()
            self._set_cors()
            self.wfile.write(
                json.dumps({"path": base}, ensure_ascii=False).encode("utf-8")
            )
        elif self.path == "/api/balance/refresh":
            cfg = load_config()
            profiles = cfg.get("profiles") or {}
            active_key = cfg.get("active_profile", "main")
            active_data = profiles.get(active_key, {}) or {}
            balance = get_live_balance(
                active_data.get("api_key", ""),
                active_data.get("base_url", ""),
                force=True,
            )
            self._set_cors()
            self.wfile.write(
                json.dumps({"balance": balance}, ensure_ascii=False).encode("utf-8")
            )
        elif self.path.startswith("/api/logs"):
            self._set_cors()
            self.wfile.write(
                json.dumps({"logs": list(LOG_BUFFER)}, ensure_ascii=False).encode("utf-8")
            )
        elif self.path.startswith("/api/progress"):
            # Живой прогресс генерации: фронтенд опрашивает во время /api/chat,
            # чтобы плашка Thinking process показывала инструменты в реальном времени.
            query = urllib.parse.urlparse(self.path).query
            progress_sid = (urllib.parse.parse_qs(query).get("session_id") or [""])[0]
            progress_steps = LIVE_THINKING.get(progress_sid)
            progress_reasoning = LIVE_REASONING.get(progress_sid, "")
            self._set_cors()
            self.wfile.write(
                json.dumps(
                    {
                        "active": progress_steps is not None,
                        "steps": list(progress_steps) if progress_steps else [],
                        "reasoning": progress_reasoning or "",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        elif self.path == "/api/backups":
            backup_dir = str(BACKUP_DIR)
            try:
                os.makedirs(backup_dir, exist_ok=True)
                backups = [
                    name for name in os.listdir(backup_dir)
                    if name.startswith("backup_") and name.endswith(".zip")
                ]
                backups.sort(reverse=True)
                self._set_cors()
                self.wfile.write(json.dumps({"count": len(backups), "backups": backups}, ensure_ascii=False).encode("utf-8"))
            except OSError as exc:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
        else:
            self._set_cors(404)

    def do_POST(self):
        if not self._authorized():
            return
        try:
            request_length(self.headers, upload=self.path == "/api/upload")
        except (ValueError, OverflowError) as exc:
            self._set_cors(413 if isinstance(exc, OverflowError) else 400)
            self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
            self.close_connection = True
            return
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as exc:
            # Клиент (например, WebView) мог закрыть запрос после получения ответа.
            # Это штатная ситуация при отмене/перезапуске запроса, а не ошибка сервера.
            log_event(f"[*] Клиент закрыл соединение до отправки ответа: {exc}")
            # Живой прогресс не должен «залипать» в active=true после сброса клиента.
            LIVE_THINKING.pop(getattr(self, "_chat_session_id", None), None)
            LIVE_REASONING.pop(getattr(self, "_chat_session_id", None), None)
            CANCEL_EVENTS.pop(getattr(self, "_chat_session_id", None), None)
        except Exception as exc:
            # Чистим живой прогресс: иначе /api/progress навсегда оставался
            # бы активным для этой сессии после аварийного завершения запроса.
            LIVE_THINKING.pop(getattr(self, "_chat_session_id", None), None)
            LIVE_REASONING.pop(getattr(self, "_chat_session_id", None), None)
            CANCEL_EVENTS.pop(getattr(self, "_chat_session_id", None), None)
            # Без этого блока любая необработанная ошибка обрывала соединение
            # вообще без тела ответа — фронтенд не получал финальный ответ ассистента
            # и просто терял его. Теперь клиент всегда получает JSON с ошибкой.
            log_event(f"[!] Ошибка обработки запроса {self.path}: {exc}")
            try:
                if not getattr(self, "_headers_sent", False):
                    self._set_cors(500)
                    self.wfile.write(
                        json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    )
            except Exception:
                pass

    def _do_POST(self):
        if self.path.startswith("/api/generated/") and self.path.endswith("/open"):
            file_id = self.path.split("/api/generated/", 1)[1].split("/", 1)[0]
            gen_path, _gen_name = _generated_file(file_id)
            if not gen_path:
                self._set_cors(404)
                self.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
                return
            if Path(gen_path).suffix.lower() not in {".docx", ".txt", ".md", ".csv", ".pdf", ".xlsx"}:
                self._set_cors(403)
                self.wfile.write(b'{"error":"This file type cannot be opened automatically"}')
                return
            try:
                if os.name == "nt":
                    os.startfile(gen_path)
                else:
                    subprocess.Popen(["xdg-open", gen_path])
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/stop":
            # Кнопка «стоп» во фронтенде: выставляем событие отмены для сессии.
            # Цикл генерации /api/chat проверяет его между шагами, а долгий вызов
            # LLM перестаёт ожидаться (см. _call_chat_cancellable).
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8", errors="replace")
            try:
                req_data = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                req_data = {}
            stop_sid = str((req_data or {}).get("session_id") or "")
            stop_event = CANCEL_EVENTS.get(stop_sid)
            if stop_event is not None:
                stop_event.set()
                log_event(f"[STOP] Запрошена остановка генерации (сессия '{stop_sid}')")
            self._set_cors()
            self.wfile.write(
                json.dumps(
                    {"ok": True, "stopped": stop_event is not None},
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            return

        if self.path in ("/api/os-tools/approve", "/api/os-tools/reject"):
            import win_tools

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8", errors="replace")
            try:
                req_data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                req_data = {}
            action_id = str((req_data or {}).get("id") or "").strip()
            if self.path.endswith("/approve"):
                ok = win_tools.approve(action_id) if action_id else False
            else:
                ok = win_tools.reject(action_id) if action_id else False
            self._set_cors()
            self.wfile.write(json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/backups":
            try:
                filename, count = create_project_backup()
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True, "filename": filename, "count": count}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                log_event(f"[!] Ошибка создания бэкапа: {exc}")
                self._set_cors(500)
                self.wfile.write(json.dumps({"ok": False, "error": "Не удалось создать бэкап: %s" % exc}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/word/presets":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос сохранения пресета")
                payload = _word_save_preset_payload(req_data)
                self._set_cors()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                log_event(f"[!] /api/word/presets: {exc}")
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )
            return

        if self.path == "/api/document-studio-settings":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                incoming = json.loads(body) if body else {}
                if not isinstance(incoming, dict):
                    raise ValueError("Некорректные настройки документ-студии")

                def _num(value):
                    if value in ("", None):
                        return None
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None

                cfg = load_config()
                cfg["document_studio"] = {
                    "base_url": str(incoming.get("base_url", "") or "").strip(),
                    "api_key": retained_secret(incoming.get("api_key"), cfg.get("document_studio", {}).get("api_key"), incoming.get("clear_api_key")),
                    "model": str(incoming.get("model", "") or "").strip(),
                    "system_prompt": str(incoming.get("system_prompt", "") or ""),
                    "temperature": _num(incoming.get("temperature")),
                    "request_timeout": int(_num(incoming.get("request_timeout")) or 0),
                }
                save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )
            return

        if self.path.startswith("/api/studio/"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос студии")
                payload = _studio_handle(self.path, req_data)
                self._set_cors()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                log_event(f"[!] {self.path}: {exc}")
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )
            return

        if self.path == "/api/proxy-settings":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                mode = str(req_data.get("mode", "auto")).strip().lower() or "auto"
                if mode not in ("auto", "direct", "custom"):
                    raise ValueError(f"неизвестный режим: {mode}")
                address = str(req_data.get("url", "") or "").strip()
                username = str(req_data.get("username", "") or "").strip()
                previous = load_config()
                old_password = previous.get("proxy_pass") or urllib.parse.urlparse(previous.get("proxy_url", "")).password
                password = retained_secret(req_data.get("password"), old_password, req_data.get("clear_password"))
                timezone = str(req_data.get("timezone", "") or "").strip()
                locale = str(req_data.get("locale", "") or "").strip()
                accept_language = str(req_data.get("accept_language", "") or "").strip()
                if mode == "custom" and not address:
                    raise ValueError("Адрес прокси обязателен")

                from network_agent.proxy_env import assemble_proxy

                full_url = assemble_proxy(address, username, password)
                cfg = load_config()
                cfg["proxy_mode"] = mode
                cfg["proxy_url"] = full_url if mode == "custom" else ""
                cfg["proxy_user"] = username if mode == "custom" else ""
                cfg["proxy_pass"] = password if mode == "custom" else ""
                cfg["proxy_timezone"] = timezone or "Europe/Tallinn"
                cfg["proxy_locale"] = locale or "et-EE"
                cfg["proxy_accept_language"] = accept_language
                save_config(cfg)

                applied = False
                verified = True
                detail = ""
                try:
                    # Ленивый импорт: тянет playwright при первом обращении.
                    from network_agent.core import agent_core

                    result = agent_core.apply_proxy_settings(
                        mode,
                        address,
                        username,
                        password,
                        timezone,
                        locale,
                        accept_language,
                        log_cb=lambda msg: log_event(f"[NET] {msg}"),
                    )
                    applied = True
                    verified = bool(result.get("verified"))
                    detail = str(result.get("detail", ""))
                except Exception as exc:
                    detail = (
                        "настройки сохранены, агент ещё не запущен — применятся "
                        f"при первом чтении страницы ({exc})"
                    )
                self._set_cors()
                self.wfile.write(
                    json.dumps(
                        {
                            "ok": True,
                            "mode": mode,
                            "applied": applied,
                            "verified": verified,
                            "detail": detail,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/file-click-mode":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос режима файлов")
                mode = str(req_data.get("mode", "")).strip().lower()
                if mode not in ("download", "preview"):
                    raise ValueError("Неизвестный режим: %s" % mode)
                cfg = load_config()
                cfg["file_click_mode"] = mode
                save_config(cfg)
                self._set_cors()
                self.wfile.write(
                    json.dumps({"ok": True, "mode": mode}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/tool-send-mode":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос режима отправки тулов")
                mode = str(req_data.get("mode", "gated") or "gated").strip().lower()
                if mode not in ("gated", "all", "embedding"):
                    mode = "gated"
                cfg = load_config()
                cfg["tool_send_mode"] = mode
                save_config(cfg)
                self._set_cors()
                self.wfile.write(
                    json.dumps({"ok": True, "mode": mode}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as exc:  # noqa: BLE001
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )
            return

        if self.path == "/api/download-base-path":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос пути скачивания")
                raw = str(req_data.get("path", "") or "").strip()
                base = os.path.expanduser(raw).strip() if raw else ""
                cfg = load_config()
                if base:
                    cfg["download_base_path"] = base
                else:
                    cfg.pop("download_base_path", None)
                save_config(cfg)
                self._set_cors()
                self.wfile.write(
                    json.dumps({"ok": True, "path": base}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/search-settings":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                incoming = json.loads(body)
                if not isinstance(incoming, dict):
                    raise ValueError("Некорректные настройки поиска")
                cfg = load_config()
                cfg["search_api_key"] = retained_secret(incoming.get("search_api_key"), cfg.get("search_api_key"), incoming.get("clear_search_api_key"))
                save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/prompt-generator-settings":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                incoming = json.loads(body)
                if not isinstance(incoming, dict):
                    raise ValueError("Некорректные настройки генератора")
                cfg = load_config()
                cfg["prompt_generator"] = {
                    "base_url": str(incoming.get("base_url", "")),
                    "api_key": retained_secret(incoming.get("api_key"), cfg.get("prompt_generator", {}).get("api_key"), incoming.get("clear_api_key")),
                    "model": str(incoming.get("model", "")),
                    "system_prompt": str(incoming.get("system_prompt", "")),
                    "idea": str(incoming.get("idea", "")),
                }
                save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/upload":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 512 * 1024 * 1024:
                self._set_cors(413)
                self.wfile.write(json.dumps({"error": "Размер файла превышает 512 МБ"}, ensure_ascii=False).encode("utf-8"))
                return
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": "Ожидается multipart/form-data"}, ensure_ascii=False).encode("utf-8"))
                return
            raw_body = self.rfile.read(content_length)
            header_message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8") + raw_body
            )
            # iter_attachments() НЕЛЬЗЯ использовать: он считает первую text/*-часть
            # «телом письма» и пропускает её — txt/md/py/csv-файлы (и файлы без
            # явного Content-Type) не находились и вложение молча отклонялось.
            upload_part = None
            first_payload_part = None
            for part in header_message.walk():
                if part.is_multipart():
                    continue
                payload_bytes = part.get_payload(decode=True)
                if payload_bytes is None:
                    continue
                if first_payload_part is None:
                    first_payload_part = part
                if part.get_filename():
                    upload_part = part
                    break
            if upload_part is None:
                upload_part = first_payload_part
            if upload_part is None:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
                return
            original_name = upload_part.get_filename() or "attachment"
            safe_name = os.path.basename(original_name).replace("\x00", "") or "attachment"
            upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
            os.makedirs(upload_dir, exist_ok=True)
            file_id = uuid.uuid4().hex
            stored_path = os.path.join(upload_dir, file_id + "_" + safe_name)
            with open(stored_path, "wb") as uploaded_file:
                uploaded_file.write(upload_part.get_payload(decode=True))
            self._set_cors()
            self.wfile.write(json.dumps({"file_id": file_id, "name": safe_name, "mime": upload_part.get_content_type()}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/save-file":
            # Нативное «Сохранить как»: в WebView2 скриптовое скачивание blob-ссылки
            # может молча игнорироваться, поэтому файл сохраняет сам бэкенд.
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос сохранения")
                reference = str(req_data.get("path") or req_data.get("file_id") or "")
                source_path = _uploaded_document_path(reference)
                if not source_path:
                    # Файл, созданный моделью (rab://download/<id>) — лежит в generated_files/.
                    generated_path, _generated_name = _generated_file(reference)
                    source_path = generated_path
                if not source_path:
                    raise FileNotFoundError("Файл для сохранения не найден")
                suggested = os.path.basename(
                    str(req_data.get("name") or os.path.basename(source_path))
                ) or "document.docx"

                # Базовый путь скачивания из настроек: если задан — сохраняем туда
                # сразу, без диалога «Сохранить как». При сбое падаем на диалог.
                base_dir = os.path.expanduser(
                    str(load_config().get("download_base_path", "") or "").strip()
                )
                if base_dir:
                    try:
                        os.makedirs(base_dir, exist_ok=True)
                        auto_path = _unique_target_path(base_dir, suggested)
                        shutil.copyfile(source_path, auto_path)
                        log_event(f"[SAVE] {source_path} -> {auto_path}")
                        self._set_cors()
                        self.wfile.write(
                            json.dumps(
                                {"ok": True, "path": auto_path, "auto": True},
                                ensure_ascii=False,
                            ).encode("utf-8")
                        )
                        return
                    except Exception as save_error:  # noqa: BLE001 - падаем на диалог
                        log_event(f"[!] Авто-сохранение не удалось, открываю диалог: {save_error}")

                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                try:
                    chosen = filedialog.asksaveasfilename(
                        title="Сохранить документ",
                        initialdir=get_desktop_dir(),
                        initialfile=suggested,
                        defaultextension=os.path.splitext(suggested)[1] or ".docx",
                    )
                finally:
                    root.destroy()

                if not chosen:
                    self._set_cors()
                    self.wfile.write(
                        json.dumps({"ok": True, "canceled": True}, ensure_ascii=False).encode("utf-8")
                    )
                    return
                shutil.copyfile(source_path, chosen)
                log_event(f"[SAVE] {source_path} -> {chosen}")
                self._set_cors()
                self.wfile.write(
                    json.dumps({"ok": True, "path": chosen}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                log_event(f"[!] /api/save-file: {exc}")
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )
            return

        if self.path == "/api/pick-files":
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected_paths = filedialog.askopenfilenames(
                initialdir=get_desktop_dir(),
                title="Выберите файлы для отправки",
                filetypes=[
                    (
                        "Все поддерживаемые",
                        "*.png *.jpg *.jpeg *.webp *.mp4 *.webm *.mov *.mkv *.txt *.py *.md *.json *.pdf *.docx *.xlsx *.csv",
                    ),
                    ("Видео", "*.mp4 *.webm *.mov *.mkv"),
                    ("Все файлы", "*.*"),
                ],
            )
            root.destroy()

            files_info = []
            for p in selected_paths:
                p = FILE_ACCESS.grant(p)
                files_info.append(
                    {"path": p, "name": os.path.basename(p), "size": os.path.getsize(p)}
                )

            self._set_cors()
            self.wfile.write(
                json.dumps({"files": files_info}, ensure_ascii=False).encode("utf-8")
            )
            return

        if self.path == "/api/pick-directory":
            try:
                chosen_dir = _pick_directory_native(get_desktop_dir())
                self._set_cors()
                self.wfile.write(
                    json.dumps({"path": chosen_dir or ""}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as exc:  # noqa: BLE001 — диалог не должен ронять запрос
                log_event(f"[!] /api/pick-directory: {exc}")
                self._set_cors(500)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )
            return

        if self.path == "/api/prompt-generator-settings":
            cfg = load_config()
            settings = cfg.get("prompt_generator", {})
            if not isinstance(settings, dict):
                settings = {}
            self._set_cors()
            self.wfile.write(json.dumps(settings, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/prompt-generator":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                base_url = str(req_data.get("base_url", "")).strip()
                api_key = str(req_data.get("api_key") or load_config().get("prompt_generator", {}).get("api_key", "")).strip()
                model = str(req_data.get("model", "")).strip()
                idea = str(req_data.get("idea", "")).strip()
                system_prompt = str(req_data.get("system_prompt", "")).strip()
                if not all((base_url, api_key, model, idea)):
                    raise ValueError("Заполните Base URL, API key, Model и описание идеи")
                cfg = load_config()
                cfg["prompt_generator"] = {
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": model,
                    "system_prompt": system_prompt,
                }
                save_config(cfg)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": f"Составь подробное техническое задание по идее: {idea}"})
                response = api_client.call_chat_api(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    temperature=0.7,
                    timeout=60,
                )
                text = response.get("content", "") if isinstance(response, dict) else str(response)
                if not text:
                    raise ValueError("Модель вернула пустой ответ")
                self._set_cors()
                self.wfile.write(json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        if self.path == "/api/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            # JSON чата содержит только текст и ID временных файлов.
            max_request_bytes = 2 * 1024 * 1024
            if content_length > max_request_bytes:
                self.send_response(413)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Размер запроса превышает 512 МБ"}, ensure_ascii=False).encode("utf-8"))
                return
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": f"Некорректный JSON: {exc}"}, ensure_ascii=False).encode("utf-8"))
                return

            session_id = req_data.get("session_id", "default")
            session_id = str(session_id or "default")
            # Document Studio идёт по собственным профилям и собственному хранилищу
            # чатов — режим определяется здесь, до чтения меты и истории.
            studio_mode = bool(req_data.get("studio"))
            studio_persona_id = str(req_data.get("studio_persona_id") or "").strip()
            # Живой прогресс регистрируется сразу (фронтенд увидит active=true
            # с первых секунд запроса); аварийная очистка — в обработчиках
            # исключений do_POST, штатная — после отправки ответа.
            self._chat_session_id = session_id
            live_steps: list = []
            LIVE_THINKING[session_id] = live_steps
            LIVE_REASONING[session_id] = ""
            # Событие отмены для POST /api/stop: выставляется кнопкой «стоп»,
            # проверяется между шагами генерации и на ожидании долгих API-вызовов.
            cancel_event = threading.Event()
            CANCEL_EVENTS[session_id] = cancel_event

            def _cancel_requested() -> bool:
                return cancel_event.is_set()
            # Чат мог быть удалён до начала обработки запроса (устаревший клиент).
            _session_deleted = session_id in DELETED_SESSIONS
            user_prompt = req_data.get("prompt", "")
            persona_id = req_data.get("persona_id")
            attachments = req_data.get("attachments", [])
            if not isinstance(attachments, list):
                attachments = []

            found_attachments = []
            direct_data_urls = []
            attachment_notes = []

            for item in attachments:
                # Веб-клиент передаёт только ID временного файла, а не содержимое файла.
                if isinstance(item, dict):
                    file_path = item.get("path")
                    file_id = item.get("file_id")
                    file_url = item.get("url", "")
                    if file_id:
                        upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
                        matches = [name for name in os.listdir(upload_dir)] if os.path.isdir(upload_dir) else []
                        matched = next((name for name in matches if name.startswith(str(file_id) + "_")), None)
                        if matched:
                            found_attachments.append(os.path.abspath(os.path.join(upload_dir, matched)))
                        else:
                            attachment_notes.append(f"{item.get('name', 'файл')}: временный файл не найден")
                    elif file_path and FILE_ACCESS.allowed(relocated_user_file(file_path)):
                        found_attachments.append(os.path.abspath(relocated_user_file(file_path)))
                    elif file_url.startswith("data:image/") or file_url.startswith("data:video/"):
                        direct_data_urls.append(file_url)
                    elif file_url.startswith("data:"):
                        attachment_notes.append(f"{item.get('name', 'файл')}: неподдержанный Data URL")
                # Если передана строка (путь на диске или Data URL)
                elif isinstance(item, str):
                    if os.path.exists(item):
                        found_attachments.append(os.path.abspath(item))
                    elif item.startswith("data:image/") or item.startswith("data:video/"):
                        direct_data_urls.append(item)
                    elif item.startswith("data:"):
                        attachment_notes.append("вложение: неподдержанный Data URL")
                    else:
                        attachment_notes.append(f"{item}: файл не найден или путь недоступен")

            for line in user_prompt.splitlines():
                if "Sent an attachment:" in line or "Отправлен файл:" in line:
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        fname = parts[1].strip()
                        if fname and os.path.exists(fname):
                            found_attachments.append(os.path.abspath(fname))

            cfg = load_config()
            profiles = cfg.get("profiles", {})

            if persona_id and persona_id in profiles:
                profile = profiles[persona_id]
                if cfg.get("active_profile") != persona_id:
                    cfg["active_profile"] = persona_id
                    save_config(cfg)
            else:
                profile = profiles.get(cfg.get("active_profile", "main"), {})

            api_base = profile.get("base_url") or profile.get(
                "api_base", "https://sprutdock.ru/v1"
            )
            api_key = profile.get("api_key", "")
            model = profile.get("model", "google/gemini-3.8-flash")
            system_prompt = profile.get("system_prompt", "")
            temperature = float(profile.get("temperature", 0.7))
            # Таймаут генерации настраивается в профиле (request_timeout);
            # connect-фаза в api_client короткая и не зависит от него.
            chat_timeout = max(10, int(profile.get("request_timeout", 120) or 120))

            # Document Studio: параметры берём из ОТДЕЛЬНОГО профиля студии.
            # Приоритет: профиль из запроса → активный профиль студии → значения
            # активного основного профиля (для незаполненных полей).
            if studio_mode:
                ensure_studio_profiles()
                cfg = load_config()
                studio_profiles = {
                    name: data
                    for name, data in (cfg.get("profiles") or {}).items()
                    if _is_studio_profile(data)
                }
                sp_name = studio_persona_id or str(cfg.get(STUDIO_ACTIVE_KEY) or "")
                studio_profile = studio_profiles.get(sp_name) or {}
                if studio_profile:
                    api_base = studio_profile.get("base_url") or api_base
                    api_key = studio_profile.get("api_key") or api_key
                    model = studio_profile.get("model") or model
                    if str(studio_profile.get("system_prompt") or "").strip():
                        system_prompt = str(studio_profile.get("system_prompt"))
                    if isinstance(studio_profile.get("temperature"), (int, float)):
                        temperature = float(studio_profile["temperature"])
                    if (
                        isinstance(studio_profile.get("request_timeout"), (int, float))
                        and studio_profile["request_timeout"]
                    ):
                        chat_timeout = max(10, int(studio_profile["request_timeout"]))
                log_event(
                    f"[STUDIO] профиль '{sp_name or '—'}': модель '{model}', "
                    f"ключ {'задан' if api_key else 'не задан'}"
                )


            log_event(f"[CHAT] Сессия '{session_id}', профиль '{persona_id or cfg.get('active_profile', 'main')}', модель '{model}'")

            # --- привязка сессии к профилю + заголовок (чтобы пережить перезагрузку) ---
            # У студии своё хранилище: мета и история не смешиваются с основными чатами.
            _session_deleted = (
                session_id in DELETED_STUDIO_SESSIONS
                if studio_mode
                else session_id in DELETED_SESSIONS
            )
            _load_meta = load_studio_sessions_meta if studio_mode else load_sessions_meta
            _save_meta = save_studio_sessions_meta if studio_mode else save_sessions_meta
            _meta_memory = studio_sessions_meta_memory if studio_mode else sessions_meta_memory
            try:
                _meta = _load_meta()
            except Exception:
                _meta = {}
            if not isinstance(_meta, dict):
                _meta = {}
            if studio_mode:
                _active_pid = studio_persona_id or str(cfg.get(STUDIO_ACTIVE_KEY) or "")
            else:
                _active_pid = persona_id or cfg.get("active_profile", "main")
            _cur = _meta.get(session_id) or {}
            _title = (req_data.get("title") or _cur.get("title") or "").strip()
            if not _title or _title in ("Новый чат", "New chat"):
                _title = _meta_title_from_prompt(user_prompt)
                # не перезаписываем осмысленный заголовок повторными сообщениями
                if _cur.get("title") and _cur.get("title") not in ("Новый чат", "New chat"):
                    _title = _cur.get("title")
            if not _session_deleted:
                _meta[session_id] = {
                    "personaId": _active_pid,
                    "title": _title,
                    "timestamp": datetime.now().strftime("%d.%m %H:%M"),
                }
                _meta_memory[session_id] = _meta[session_id]
                _save_meta(_meta)

            active_sys_prompt = (system_prompt or "").strip()
            # Дата добавляется при каждом запросе, чтобы не устаревать в долгих сессиях.
            # Это не справочная подсказка, а обязательное правило интерпретации времени.
            _today = datetime.now()
            _DATE_INSTRUCTION = (
                f"\n\n[ДАТА]\n"
                f"Текущая локальная дата и время: {_today.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(дата: {_today.strftime('%d.%m.%Y')}). "
                "Считай её единственным источником истины для слов «сегодня», «вчера», "
                "«завтра» и других относительных дат. Не называй дату из обучающих данных текущей."
            )
            active_sys_prompt += _DATE_INSTRUCTION
            # Подсказка про панель «Заметки». ВАЖНО: в системный промпт она
            # больше НЕ вклеивается безусловно. Раньше модель на каждом ходу
            # читала про save_note даже тогда, когда гейт tools не отправлял,
            # и начинала «вызывать» инструмент текстом (галлюцинация вызова).
            # Теперь подсказку добавляет tool_gating.apply_tool_prompt — и только
            # если save_note действительно есть в payload этого хода.
            _NOTES_HINT = (
                "[Панель «Заметки»: save_note(title, content) сохраняет текст, list_notes() показывает. "
                "Заметки — НЕ память профиля. Если сохранять нечего — отвечай текстом.]"
            )

            # Память профиля: ДО обращения к модели достаём из локального
            # хранилища (LanceDB + FastEmbed) факты о пользователе, извлечённые
            # из прошлых диалогов, и подмешиваем их в системный промпт. Блок
            # живёт в chat_history[0], поэтому виден и на следующих шагах
            # tool-цикла. Любой сбой памяти не должен ломать ответ.
            _memory_started = time.perf_counter()
            # Для тривиальных реплик («привет», «ок», «спасибо») факты профиля
            # бессмысленны: они раздувают промпт и провоцируют модель рассуждать,
            # применимы ли известные ей сведения к приветствию. Пропуск векторного
            # поиска экономит и время, и токены. Флаг тот же, что у гейта tools.
            _skip_memory = (
                bool(cfg.get("skip_search_on_smalltalk", True))
                and tool_gating.is_trivial(user_prompt)
            )
            try:
                _memory_block = (
                    user_profile_memory.build_memory_context(user_prompt)
                    if (user_profile_memory is not None and not _skip_memory)
                    else ""
                )
            except Exception as _mem_error:
                _memory_block = ""
                log_event(f"[!] Память профиля недоступна (не критично): {_mem_error}")
            if _skip_memory:
                log_event("[MEMORY] Пропущено: тривиальная реплика, факты профиля не нужны")
            if _memory_block:
                active_sys_prompt = (
                    (active_sys_prompt + _memory_block)
                    if active_sys_prompt
                    else _memory_block.strip()
                )
                _facts_count = _memory_block.count("\n- (")
                live_steps.append(
                    f"🧠 Память профиля (локальная база, не вызов инструмента): фактов — {_facts_count}"
                )
                log_event(
                    f"[MEMORY] Фактов профиля в контексте: {_facts_count} "
                    f"({(time.perf_counter() - _memory_started) * 1000:.0f} мс)"
                )

            # chat_history — локальная ссылка на историю текущего запроса.
            # Если чат удалён (в т.ч. пока шёл долгий ответ ассистента), работаем
            # во временном списке и ничего не пишем на диск.
            if _session_deleted:
                chat_history: list = []
            else:
                if studio_mode:
                    _history_mem = studio_sessions_memory
                    if session_id not in _history_mem:
                        _history_mem[session_id] = load_studio_history(session_id)
                else:
                    _history_mem = sessions_memory
                    if session_id not in _history_mem:
                        _history_mem[session_id] = load_history().get(session_id, [])
                chat_history = _history_mem[session_id]

            def _persist_history() -> None:
                """Сохраняет историю текущего чата в своё хранилище (студия/основное)."""
                try:
                    if studio_mode:
                        save_studio_history(session_id, chat_history)
                    else:
                        save_history(sessions_memory)
                except Exception as _save_error:
                    log_event(f"[!] Не удалось сохранить историю чата: {_save_error}")
            if not chat_history and active_sys_prompt:
                chat_history.append({"role": "system", "content": active_sys_prompt})
            elif (
                chat_history
                and isinstance(chat_history[0], dict)
                and chat_history[0].get("role") == "system"
                and active_sys_prompt
            ):
                chat_history[0]["content"] = active_sys_prompt

            final_user_prompt = user_prompt
            images = []
            files_text = ""

            # Лимиты вложений читаются из профиля в реальном времени: перед каждым
            # build_payload кладём их в окружение, откуда attach_tools их подхватывает.
            os.environ.update({
                "ATTACH_MAX_IMAGES": str(profile.get("attach_max_images", 4)),
                "ATTACH_MAX_TEXT_CHARS": str(profile.get("attach_max_text_chars", 60000)),
                "ATTACH_MAX_TEXT_PER_FILE": str(profile.get("attach_max_text_per_file", 60000)),
                "ATTACH_MAX_TOTAL_TEXT": str(profile.get("attach_max_total_text", 180000)),
                "ATTACH_CSV_MAX_ROWS": str(profile.get("attach_csv_max_rows", 400)),
                "ATTACH_XLSX_MAX_ROWS": str(profile.get("attach_xlsx_max_rows", 400)),
                "CHAT_MSG_MAX_CHARS": str(profile.get("chat_msg_max_chars", 0)),
                "CHAT_MSG_TEXT_PART_MAX_CHARS": str(profile.get("chat_msg_text_part_max_chars", 0)),
            })

            if found_attachments:
                imgs_from_disk, files_text, build_notes = attach_tools.build_payload(found_attachments)
                images.extend(imgs_from_disk)
                attachment_notes.extend(build_notes)

            # Data URL оставлен только для обратной совместимости со старыми клиентами.
            # Новый веб-клиент всегда использует /api/upload и передаёт file_id.
            if direct_data_urls:
                images.extend(direct_data_urls)

            if attachment_notes:
                notes_text = "\n".join(f"- {note}" for note in attachment_notes)
                final_user_prompt = f"[Сведения о вложениях]\n{notes_text}\n[/Сведения о вложениях]\n\n" + final_user_prompt
            if files_text:
                final_user_prompt = files_text + "\n\n" + final_user_prompt

            # Начало текущего хода в истории. Если провайдер отклонит запрос из-за
            # фильтра контента (Content policy violation), откатим всё, что добавилось
            # за этот ход: иначе «отравленная» реплика останется в контексте и каждый
            # следующий запрос (даже короткое «1») будет снова отклонён.
            turn_start_index = len(chat_history)

            if images:
                parts = [{"type": "text", "text": final_user_prompt}]
                for du in images:
                    if du.startswith("data:video/"):
                        parts.append({"type": "video_url", "video_url": {"url": du}})
                    else:
                        parts.append({"type": "image_url", "image_url": {"url": du}})
                chat_history.append(
                    {"role": "user", "content": parts}
                )
            else:
                chat_history.append(
                    {"role": "user", "content": final_user_prompt}
                )

            # Сохраняем пользовательское сообщение сразу, не дожидаясь ответа API.
            # Поэтому оно не теряется при перезапуске или падении приложения.
            # ВАЖНО: content последнего сообщения НЕ перезаписываем — там промпт
            # с содержимым файлов/картинками (final_user_prompt), он уходит в API.
            # Раньше update() затирал content сырым промптом, и вложения не
            # доходили до модели в обычном режиме. Для отображения храним
            # отдельные ключи: display (сырой текст) и attachments (имена+ссылки).
            if not _session_deleted:
                _attach_meta = [
                    {
                        "name": str(item.get("name", "вложение")),
                        "url": f"/api/download/{item.get('file_id')}" if item.get("file_id") else "",
                    }
                    for item in attachments
                    if isinstance(item, dict) and item.get("file_id")
                ]
                _display_text = (user_prompt or "").strip()
                if not _display_text and _attach_meta:
                    _display_text = "Отправлены файлы: " + ", ".join(a["name"] for a in _attach_meta)
                if chat_history and isinstance(chat_history[-1], dict):
                    chat_history[-1]["display"] = _display_text
                    chat_history[-1]["attachments"] = _attach_meta
                _persist_history()

            # live_steps — тот же объект, что и thinking_steps: каждый шаг
            # (поиск, чтение страницы, план архитектора) сразу виден во
            # фронтенд-опросе /api/progress.
            thinking_steps = live_steps
            # Накопленный текст «размышлений» модели за весь ход (reasoning content /
            # thought parts): сериализуется отдельным полем в сообщение ассистента
            # и возвращается фронту как reasoning (плюс живьём в /api/progress).
            reasoning_acc = ""
            final_reply = ""
            assistant_notes = []

            def _on_thinking(delta_text):
                """Льёт reasoning-токены в живой виджет Thinking process."""
                nonlocal reasoning_acc
                reasoning_acc += delta_text
                LIVE_REASONING[session_id] = LIVE_REASONING.get(session_id, "") + delta_text

            try:
                tool_loops = 0
                # Лимит шагов tool-цикла. Раньше здесь было None, поэтому
                # условие `while max_tool_loops is None or ...` не имело
                # границы, а max_steps из UI (page.tsx:753) и из профиля
                # игнорировался: модель могла звать инструменты бесконечно
                # (в истории есть ходы по 12-13 вызовов web_search).
                try:
                    max_tool_loops = max(1, min(200, int(
                        req_data.get("max_steps")
                        or profile.get("max_steps", 60)
                        or 60
                    )))
                except Exception:
                    max_tool_loops = 60
                # Поле max_history_messages (есть в config.json, ранее
                # использовалось только старым клиентом): ограничивает
                # размер промпта — латентность и цена на длинных чатах.
                history_cap = int(cfg.get("max_history_messages", 20) or 0) or None

                # ГЕЙТКИПЕР: решаем, какие инструменты вообще отправлять.
                # Пустой список => tools и tool_choice не попадут в payload,
                # и модель физически не сможет вызвать функцию. Это единственный
                # надёжный рычаг: поведение прокси при трансляции tool_choice
                # в нативный Gemini (function_calling_config) непроверяемо.
                # Вложения — всегда повод оставить инструменты (gate off).
                # Document Studio: вместо поиска — инструменты редактора DOCX.
                studio_hint = _STUDIO_HINT
                if studio_mode:
                    # К чату может быть приложено несколько пресетов — все
                    # валидируются и уходят в системный промпт для анализа.
                    from services.word_automation import load_preset as _load_preset

                    incoming = req_data.get("presets")
                    raw_presets = (
                        [p for p in incoming if isinstance(p, dict)]
                        if isinstance(incoming, list)
                        else []
                    )
                    single_preset = req_data.get("preset")
                    if isinstance(single_preset, dict):
                        raw_presets.append(single_preset)
                    valid_presets = []
                    for item in raw_presets:
                        try:
                            loaded = _load_preset(item)
                            data = loaded.model_dump(mode="json")
                            data["id"] = loaded.name
                            valid_presets.append(data)
                        except Exception as exc:  # noqa: BLE001 - битый пресет не рушит чат
                            log_event(f"[!] Пресет из чата студии не принят: {exc}")
                    if valid_presets:
                        _studio_set_presets(session_id, valid_presets)
                    attached = _studio_state(session_id).get("attached_presets") or []
                    if attached:
                        names = ", ".join(
                            str(p.get("name") or p.get("id") or "?") for p in attached
                        )
                        studio_hint = (
                            _STUDIO_HINT
                            + f"\n\nК чату приложено пресетов: {len(attached)} ({names}). "
                            "Ниже их JSON — анализируй оформление и можешь сравнивать "
                            "пресеты между собой:\n"
                            + json.dumps(attached, ensure_ascii=False)[:8000]
                        )
                    turn_tools = list(api_client.WORD_TOOLS_SCHEMA)
                    log_event(
                        f"[STUDIO] tools в payload: {len(turn_tools)} "
                        f"{tool_gating.tool_names(turn_tools)}"
                    )
                else:
                    _tool_mode = str(cfg.get("tool_send_mode", "gated") or "gated").strip().lower()
                    if _tool_mode == "all":
                        # Режим 2: всегда все тулы, модель решает сама.
                        turn_tools = list(api_client.TOOLS_SCHEMA)
                    elif _tool_mode == "embedding":
                        # Режим 3: намерение определяет эмбеддинг-роутер.
                        turn_tools = tool_gating.select_tools_by_embedding(
                            user_text=final_user_prompt,
                            all_tools=api_client.TOOLS_SCHEMA,
                            history=chat_history,
                        )
                    else:
                        # Режим 1: маркерный гейт (как раньше).
                        _gate_enabled = (
                            bool(cfg.get("skip_search_on_smalltalk", True))
                            and not (attachments or images or files_text)
                        )
                        turn_tools = tool_gating.select_tools(
                            user_text=final_user_prompt,
                            all_tools=api_client.TOOLS_SCHEMA,
                            history=chat_history,
                            allow_retrieval=True,
                            gate_enabled=_gate_enabled,
                        )
                    log_event(
                        f"[GATE] tools в payload: {len(turn_tools)} "
                        f"{tool_gating.tool_names(turn_tools) or '— отключены —'} | "
                        f"тривиально={tool_gating.is_trivial(final_user_prompt)} "
                        f"поиск={tool_gating.needs_retrieval(final_user_prompt)} "
                        f"действия={list(tool_gating.needed_action_tools(final_user_prompt))} "
                        f"лимит_шагов={max_tool_loops}"
                    )
                if not turn_tools:
                    # Формулировка важна: это НЕ вызов инструмента, а признак
                    # того, что в запрос вообще не положена схема tools.
                    live_steps.append(
                        "🚫 Инструменты в запрос не отправлялись "
                        "(реплика не требует ни поиска, ни сохранения)"
                    )

                # Анти-цикл: идентичный вызов не должен повторяться
                _seen_calls = set()

                while tool_loops < max_tool_loops:
                    # Пользователь нажал «стоп» — новый запрос не начинаем.
                    if cancel_event.is_set():
                        raise _GenerationStopped()
                    safe_messages = _prepare_messages_for_api(
                        chat_history, None, max_msgs=history_cap
                    )
                    # Промпт приводим в соответствие с фактическим payload:
                    # tools нет — вырезаем упоминания инструментов, чтобы модель
                    # не «вызывала» их текстом; tools есть — добавляем подсказку
                    # про Заметки и «ПОРЯДОК РЕШЕНИЯ». Правится копия для API,
                    # история на диске и профиль остаются нетронутыми.
                    safe_messages = tool_gating.apply_tool_prompt(
                        safe_messages,
                        turn_tools,
                        extra_hint=studio_hint if studio_mode else _NOTES_HINT,
                    )

                    # Отменяемый стриминговый вызов: reasoning-токены льются в
                    # _on_thinking (живой виджет), при «стопе» чтение прерывается.
                    resp = _call_chat_cancellable(
                        cancel_event,
                        timeout=chat_timeout,
                        stream=True,
                        on_thinking=_on_thinking,
                        base_url=api_base,
                        api_key=api_key,
                        model=model,
                        messages=safe_messages,
                        tools=turn_tools or None,
                        temperature=temperature,
                    )
                    if resp is None:
                        raise _GenerationStopped()

                    has_tool_calls = (
                        isinstance(resp, dict) and bool(resp.get("tool_calls"))
                    ) or (hasattr(resp, "tool_calls") and bool(getattr(resp, "tool_calls")))

                    # Если модель вернула текст без вызовов инструментов — выходим, ответ готов
                    if not has_tool_calls:
                        break

                    tool_loops += 1
                    msg_obj = resp if isinstance(resp, dict) else resp.__dict__
                    tool_calls = msg_obj.get("tool_calls", [])

                    # Протокол: у каждого вызова обязан быть id. Если прокси
                    # его не вернул, генерим уникальный ДО записи assistant —
                    # иначе tool_call_id ответа не совпадёт с
                    # assistant.tool_calls[].id и в истории останется
                    # незакрытый вызов (модель зацикливается на нём).
                    for _tc_index, _tc in enumerate(tool_calls or []):
                        if isinstance(_tc, dict) and not _tc.get("id"):
                            _tc["id"] = f"call_{tool_loops}_{_tc_index}_{uuid.uuid4().hex[:8]}"

                    # 1. Записываем вызов ассистента
                    chat_history.append({
                        "role": "assistant",
                        "content": msg_obj.get("content") or "",
                        "tool_calls": tool_calls
                    })

                    # Последний разрешённый шаг: дальше принуждаем к тексту
                    is_final_step = tool_loops >= max_tool_loops

                    # 2. Выполняем КАЖДЫЙ вызванный тул, чтобы не нарушить протокол API
                    for tc_index, tc in enumerate(tool_calls):
                        if cancel_event.is_set():
                            raise _GenerationStopped()
                        tc_id = tc.get("id") or f"call_{tool_loops}_{tc_index}"
                        fn_info = tc.get("function", {})
                        fn_name = fn_info.get("name", "")
                        raw_args = fn_info.get("arguments", "{}")

                        try:
                            fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except Exception:
                            fn_args = {}

                        # АНТИ-ЦИКЛ: идентичный повтор того же инструмента с теми
                        # же аргументами не тратит round-trip и не даёт модели
                        # повода для нового витка (в истории были ходы по 12 вызовов).
                        try:
                            _sig_args = json.dumps(
                                fn_args, ensure_ascii=False, sort_keys=True, default=str
                            )[:300]
                        except Exception:
                            _sig_args = str(fn_args)[:300]
                        _call_sig = f"{fn_name}::{_sig_args}"
                        if _call_sig in _seen_calls:
                            log_event(
                                f"[GATE] Повторный идентичный вызов {fn_name} — цикл прерван"
                            )
                            chat_history.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": (
                                    "Этот инструмент с такими же аргументами уже вызывался. "
                                    "Больше не вызывай инструменты — сформируй итоговый "
                                    "текстовый ответ по уже полученным данным."
                                ),
                            })
                            tool_loops = max_tool_loops
                            continue
                        _seen_calls.add(_call_sig)

                        tool_output = ""
                        request_started = time.perf_counter()
                        if fn_name == "local_search":
                            query = str(fn_args.get("query", "")).strip()
                            thinking_steps.append(f"📁 Поиск в локальных документах: «{query}»")
                            try:
                                local_results = retrieval_quality.local_search(query)
                                tool_output = retrieval_quality.format_local_results(local_results)
                                retrieval_quality.record_request(bool(local_results), (time.perf_counter() - request_started) * 1000, citations=len(local_results))
                            except Exception as err:
                                tool_output = f"Ошибка локального поиска: {err}"
                                retrieval_quality.record_request(False, (time.perf_counter() - request_started) * 1000)
                        elif fn_name == "web_search":
                            query = str(fn_args.get("query", "")).strip()
                            thinking_steps.append(f"🔍 Поиск в сети: «{query}»")
                            log_event(f"[*] Выполняется веб-поиск: {query}")
                            try:
                                # web_search ВСЕГДА возвращает кортеж
                                # (results, error) — распаковываем, как в
                                # legacy_gui/chat_app.py:1313. Раньше здесь
                                # проверялся isinstance(..., list), поэтому
                                # format_search_block не вызывался никогда,
                                # а модель получала repr кортежа, принимала
                                # его за сбой инструмента и повторяла вызов.
                                search_results, search_error = search_tools.web_search(
                                    query,
                                    max_results=int(profile.get("search_results", 5) or 5),
                                    region=str(profile.get("search_region", "ru-ru") or "ru-ru"),
                                )
                                if search_results:
                                    tool_output = (
                                        search_tools.format_search_block(search_results)
                                        or "Источники найдены, но без содержимого страниц."
                                    )
                                elif search_error:
                                    tool_output = (
                                        f"Поиск не выполнен: {search_error}. "
                                        "Не повторяй этот вызов — отвечай по имеющимся данным."
                                    )
                                else:
                                    tool_output = (
                                        "По данному запросу ничего не найдено. "
                                        "Не повторяй вызов — честно скажи, что данных нет."
                                    )
                                retrieval_quality.record_request(
                                    bool(search_results),
                                    (time.perf_counter() - request_started) * 1000,
                                    citations=len(search_results) if isinstance(search_results, list) else 0,
                                )
                            except Exception as err:
                                tool_output = f"Ошибка поиска: {err}. Не повторяй этот вызов."
                                retrieval_quality.record_request(False, (time.perf_counter() - request_started) * 1000)


                        elif fn_name == "fetch_webpage":
                            url = fn_args.get("url", "")
                            thinking_steps.append(f"🌐 Чтение страницы: {url}")
                            log_event(f"[*] Чтение страницы: {url}")
                            try:
                                # Ленивый импорт: network_agent тянет playwright
                                # (тяжёлые библиотеки), который не нужен до
                                # первого обращения к веб-инструментам.
                                from network_agent.core import agent_core

                                if getattr(agent_core, "log_cb", None) is None:
                                    # Решения по прокси (системный/прямой) — в логи интерфейса
                                    agent_core.log_cb = lambda msg: log_event(f"[NET] {msg}")
                                    state = agent_core.proxy_url or "не найден — прямой доступ"
                                    agent_core.log_cb(f"🌐 Прокси: {state}")
                                tool_output = agent_core.fetch_webpage(url)
                            except Exception as err:
                                tool_output = f"Ошибка чтения страницы: {err}"

                        elif fn_name == "analyze_github_repo":
                            repo_url = str(fn_args.get("url", "") or "").strip()
                            thinking_steps.append(f"📦 Анализ репозитория: {repo_url}")
                            log_event(f"[*] Анализ репозитория GitHub: {repo_url}")
                            try:
                                import github_analyzer

                                tool_output = github_analyzer.analyze_repository(
                                    repo_url,
                                    base_url=api_base,
                                    api_key=api_key,
                                    model=model,
                                    temperature=temperature,
                                )
                            except Exception as err:
                                tool_output = f"Ошибка анализа репозитория: {err}"

                        elif fn_name == "create_downloadable_file":
                            thinking_steps.append(f"📄 Создание файла: {fn_args.get('file_name', '')}")
                            try:
                                tool_output = file_tools.create_downloadable_file(**fn_args)
                            except Exception as err:
                                tool_output = f"Ошибка создания файла: {err}"

                        elif fn_name == "save_note":
                            _nt = str(fn_args.get("title", "") or "").strip() or "Заметка от ассистента"
                            _nc = str(fn_args.get("content", "") or "").strip()
                            if not _nc:
                                tool_output = "Ошибка: пустой content заметки, нечего сохранять."
                            else:
                                if len(_nt) > 120:
                                    _nt = _nt[:120]
                                if len(_nc) > 4000:
                                    _nc = _nc[:4000] + "\n...(обрезано)..."
                                note_id = f"note_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
                                note_data = {
                                    "id": note_id,
                                    "title": _nt,
                                    "content": _nc,
                                    "createdAt": datetime.now().strftime("%d.%m.%Y, %H:%M:%S"),
                                    "author": "assistant",
                                }
                                stored_notes = load_notes()
                                stored_notes.insert(0, note_data)
                                save_notes(stored_notes)
                                assistant_notes.append({"title": _nt, "content": _nc})
                                thinking_steps.append(f"📝 Заметка сохранена: «{_nt}»")
                                log_event(f"[*] Модель сохранила заметку: {_nt}")
                                tool_output = f"Заметка «{_nt}» сохранена в панель «Заметки» и уже видна пользователю."

                        elif fn_name == "list_notes":
                            thinking_steps.append("📝 Просмотр заметок")
                            try:
                                _all_notes = load_notes()
                                if not _all_notes:
                                    tool_output = "В панели «Заметки» пока нет ни одной заметки."
                                else:
                                    _lines = []
                                    for _i, _note in enumerate(_all_notes, 1):
                                        _nt2 = str(_note.get("title") or "").strip() or "Без названия"
                                        _nc2 = str(_note.get("content") or "").strip()
                                        _lines.append(f"{_i}. {_nt2}: {_nc2}")
                                    tool_output = "Заметки из панели «Заметки»:\n" + "\n".join(_lines)
                            except Exception as _err:
                                tool_output = f"Не удалось прочитать заметки: {_err}"

                        elif fn_name == "remember_user_fact":
                            # Долговременная память профиля: факт уходит в
                            # локальный LanceDB и всплывает в следующих диалогах.
                            _fact = str(fn_args.get("fact", "") or "").strip()
                            _fact_cat = str(fn_args.get("category", "") or "").strip()
                            thinking_steps.append(f"🧠 Запоминаю факт о пользователе: {_fact[:70]}")
                            if user_profile_memory is None:
                                tool_output = (
                                    "Память профиля недоступна в этом окружении — факт не сохранён "
                                    f"({MEMORY_IMPORT_ERROR or 'модуль memory не импортирован'}). "
                                    "Продолжай отвечать как обычно и не повторяй вызов."
                                )
                                log_event(
                                    "[!] remember_user_fact отклонён: "
                                    f"{MEMORY_IMPORT_ERROR or 'модуль памяти не импортирован'}"
                                )
                            elif not _fact:
                                tool_output = "Ошибка: пустой fact, нечего сохранять в память профиля."
                            else:
                                try:
                                    _mem_result = user_profile_memory.remember_user_fact(_fact, _fact_cat)
                                    tool_output = str(_mem_result.get("message", ""))
                                    log_event(
                                        f"[MEMORY] remember_user_fact → {_mem_result.get('status')}: "
                                        f"[{_mem_result.get('category')}] {_fact[:80]}"
                                    )
                                    if not _mem_result.get("ok"):
                                        # Причина отказа (например, нет lancedb) обязана
                                        # попасть в лог, а не остаться внутри ответа модели.
                                        log_event(f"[!] Память профиля: {tool_output}")
                                    # Сразу освежаем блок памяти в системном промпте,
                                    # чтобы следующий шаг tool-цикла видел новый факт.
                                    if (
                                        _mem_result.get("ok")
                                        and chat_history
                                        and isinstance(chat_history[0], dict)
                                        and chat_history[0].get("role") == "system"
                                    ):
                                        _base_sys = user_profile_memory.strip_memory_block(
                                            chat_history[0].get("content", "")
                                        )
                                        _fresh_block = user_profile_memory.build_memory_context(
                                            user_prompt
                                        )
                                        chat_history[0]["content"] = (
                                            (_base_sys + _fresh_block) if _fresh_block else _base_sys
                                        )
                                except Exception as _mem_exc:
                                    tool_output = f"Не удалось сохранить факт в память профиля: {_mem_exc}"
                        elif fn_name == "set_system_prompt":
                            _new_prompt = str(fn_args.get("prompt", "") or "").strip()
                            if not _new_prompt:
                                tool_output = "Ошибка: пустой системный промпт, нечего сохранять."
                            else:
                                if len(_new_prompt) > 8000:
                                    _new_prompt = _new_prompt[:8000] + "\n...(обрезано)..."
                                profile["system_prompt"] = _new_prompt
                                save_config(cfg)
                                thinking_steps.append("⚙️ Системный промпт обновлён")
                                log_event(f"[*] Модель обновила системный промпт профиля ({len(_new_prompt)} симв.)")
                                tool_output = (
                                    "Системный промпт профиля обновлён. Новый промпт будет применяться "
                                    "к следующим сообщениям. Не вызывай этот инструмент повторно без новой просьбы."
                                )
                        elif fn_name == "manage_memory":
                            # Модель меняет долговременную память профиля по явному
                            # запросу пользователя: list/update/delete/clear.
                            _mem_action = str(fn_args.get("action", "") or "").strip().lower()
                            _mem_id = str(fn_args.get("fact_id", "") or "").strip()
                            _mem_fact = str(fn_args.get("fact", "") or "").strip()
                            _mem_cat = str(fn_args.get("category", "") or "").strip()
                            thinking_steps.append(f"🧠 Управление памятью профиля: {_mem_action}")
                            if user_profile_memory is None:
                                tool_output = (
                                    "Память профиля недоступна в этом окружении — операция не выполнена "
                                    f"({MEMORY_IMPORT_ERROR or 'модуль memory не импортирован'}). "
                                    "Продолжай отвечать как обычно и не повторяй вызов."
                                )
                                log_event(
                                    "[!] manage_memory отклонён: "
                                    f"{MEMORY_IMPORT_ERROR or 'модуль памяти не импортирован'}"
                                )
                            else:
                                try:
                                    _mem_result = user_profile_memory.manage_memory(
                                        _mem_action,
                                        fact_id=_mem_id,
                                        fact=_mem_fact,
                                        category=_mem_cat,
                                    )
                                    tool_output = str(_mem_result.get("message", ""))
                                    log_event(
                                        f"[MEMORY] manage_memory[{_mem_action}] → "
                                        f"{_mem_result.get('status')}"
                                        + (f": {_mem_id}" if _mem_id else "")
                                    )
                                    if not _mem_result.get("ok"):
                                        log_event(f"[!] Память профиля: {tool_output}")
                                    # Освежаем блок памяти в системном промпте, чтобы
                                    # следующие шаги tool-цикла видели изменения сразу.
                                    if (
                                        chat_history
                                        and isinstance(chat_history[0], dict)
                                        and chat_history[0].get("role") == "system"
                                    ):
                                        _base_sys = user_profile_memory.strip_memory_block(
                                            chat_history[0].get("content", "")
                                        )
                                        _fresh_block = user_profile_memory.build_memory_context(
                                            user_prompt
                                        )
                                        chat_history[0]["content"] = (
                                            (_base_sys + _fresh_block) if _fresh_block else _base_sys
                                        )
                                except Exception as _mem_exc:
                                    tool_output = f"Не удалось выполнить операцию с памятью профиля: {_mem_exc}"
                        elif fn_name in _WORD_TOOL_NAMES:
                            thinking_steps.append(f"📄 Правка документа студии: {fn_name}")
                            # Рабочий документ студии общий для всех её чатов —
                            # инструменты всегда адресуют один и тот же ключ.
                            tool_output = _run_word_tool(fn_name, fn_args, STUDIO_SESSION_ID)
                        else:
                            tool_output = f"Инструмент '{fn_name}' не поддерживается."

                        # ЖЕСТКАЯ ЗАЩИТА: режем простыни до 4000 символов (спасает баланс от HTTP 402)
                        if len(str(tool_output)) > 4000:
                            tool_output = str(tool_output)[:4000] + "\n...(содержимое обрезано для экономии токенов)..."

                        # Если лимит шагов исчерпан — принуждаем модель сформировать текст
                        if is_final_step:
                            tool_output += "\n\n[СИСТЕМА: Это последний шаг. Больше не вызывай инструменты, сформируй подробный текстовый ответ пользователю.]"

                        chat_history.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": str(tool_output)
                        })

                final_reply = ""

                if isinstance(resp, dict):
                    final_reply = resp.get("content") or resp.get("reasoning_content") or ""
                else:
                    final_reply = str(resp)

                # Модели с thinking (gemini-3.8-flash и т.п.) иногда выдают
                # размышления и в content, и в reasoning_content. Видимый ответ
                # не должен повторять блок «Thinking process» — вырезаем дубль,
                # а пустой остаток уйдёт в принудительный запрос текста ниже.
                final_reply = _strip_reasoning_from_reply(final_reply, reasoning_acc)

                # ЗАЩИТА: после последнего вызова инструмента модель иногда не успевает
                # прислать отдельное текстовое сообщение (особенно при достижении лимита
                # tool_loops), поэтому повторяем запрос без инструментов по актуальной истории.
                if not final_reply.strip():
                    log_event("[*] Модель не сформировала текстовый ответ. Принудительный запрос текста...")
                    fallback_messages = _prepare_messages_for_api(
                        chat_history, None, max_msgs=history_cap
                    )
                    # Принудительный текстовый ответ идёт без tools — значит и
                    # упоминаний инструментов в промпте быть не должно.
                    fallback_messages = tool_gating.apply_tool_prompt(
                        fallback_messages, None
                    )
                    if reasoning_acc.strip():
                        fallback_messages.append(
                            {"role": "assistant", "content": "", "reasoning": reasoning_acc}
                        )
                    fallback_messages.append({
                        "role": "user",
                        "content": (
                            "Сформируй итоговый развернутый ответ на русском языке по сути вопроса. "
                            "Используй результаты выполненных инструментов выше, не вызывай инструменты "
                            "и не описывай процесс рассуждений. Если данных недостаточно, честно укажи это."
                        ),
                    })
                    retry_resp = _call_chat_cancellable(
                        cancel_event,
                        timeout=chat_timeout,
                        stream=True,
                        on_thinking=_on_thinking,
                        base_url=api_base,
                        api_key=api_key,
                        model=model,
                        messages=fallback_messages,
                        tools=None,
                        temperature=temperature,
                    )
                    if retry_resp is None:
                        raise _GenerationStopped()
                    if isinstance(retry_resp, dict):
                        final_reply = (
                            retry_resp.get("content")
                            or retry_resp.get("reasoning_content")
                            or retry_resp.get("thought")
                            or ""
                        )
                    else:
                        final_reply = str(retry_resp or "")

                # Размышления модели уходят отдельным полем reasoning (не в шаги).
                if reasoning_acc:
                    LIVE_REASONING[session_id] = reasoning_acc

                if "<" + "think>" in final_reply and "</" + "think>" in final_reply:
                    parts = final_reply.split("</" + "think>", 1)
                    inside_think = parts[0].replace("<" + "think>", "").strip()
                    if inside_think:
                        thinking_steps.extend(
                            [s.strip() for s in inside_think.split("\n") if s.strip()]
                        )
                    final_reply = parts[1].strip()

                if cancel_event.is_set():
                    # Остановка в режиме ассистента: оборванный ответ не пишем в
                    # историю — так же, как в обычном режиме (фрагмент ответа
                    # не должен «воскреснуть» в контексте следующих сообщений).
                    log_event(f"[STOP] Генерация прервана пользователем (сессия '{session_id}')")
                else:
                    assistant_message = {"role": "assistant", "content": final_reply}
                    if reasoning_acc:
                        assistant_message["reasoning"] = reasoning_acc
                    chat_history.append(assistant_message)
                    if not _session_deleted:
                        _persist_history()
                    log_event(f"[CHAT] Ответ сформирован ({len(final_reply)} символов), шагов рассуждения: {len(thinking_steps)}")

            except _GenerationStopped:
                # Кнопка «стоп»: ответ не формируем и в историю не пишем —
                # оборванный ответ не должен «воскреснуть» в контексте чата.
                final_reply = "⏹ Генерация остановлена пользователем."
                thinking_steps = [str(s) for s in (thinking_steps or [])]
                log_event(f"[STOP] Генерация прервана пользователем (сессия '{session_id}')")
            except api_client.ToolCallWithoutToolsError:
                # Модель в no-tools запросе всё равно вернула tool_call (типично для
                # прокси-трансляции Gemini). Не роняем сырую ошибку API — объясняем
                # человеку, что пошло не так.
                final_reply = (
                    "Не удалось сформировать текстовый ответ: модель попыталась вызвать "
                    "инструмент, хотя в этом запросе инструменты не передавались. "
                    "Попробуйте переформулировать запрос или повторить."
                )
                thinking_steps = [str(s) for s in (thinking_steps or [])]
                log_event("[!] Модель вернула tool_call в no-tools запросе")
            except Exception as e:
                final_reply = f"Ошибка обращения к API: {e!s}"
                log_event(f"[!] Ошибка обращения к API: {e!s}")
                # Провайдер отклонил запрос из-за фильтра контента (Content policy
                # violation и т.п.). Фильтр находится на стороне API, а не в приложении,
                # поэтому его нельзя «выключить» отсюда. Но можно не дать чату застрять:
                # последний ход (вопрос пользователя и всё, что добавилось за него)
                # убирается из истории, чтобы следующий запрос не пересылал запрещённое
                # содержимое и не получал тот же отказ снова и снова.
                if api_client.is_content_policy_error(e):
                    # Провайдер не сообщает, какая именно фраза/слово сработала, поэтому
                    # сохраняем локальный снимок полного контекста ДО отката. По файлу
                    # можно «уполовиниванием» найти точный триггер.
                    try:
                        _snapshot = {
                            "session_id": session_id,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "messages": chat_history if isinstance(chat_history, list) else [],
                        }
                        _dbg_dir = os.path.join(
                            str(USER_DATA_DIR),
                            "history", "_content_policy_debug",
                        )
                        os.makedirs(_dbg_dir, exist_ok=True)
                        _safe_sid = "".join(
                            ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(session_id)
                        )[:80] or "session"
                        _dbg_file = os.path.join(_dbg_dir, f"{_safe_sid}_{int(time.time())}.json")
                        with open(_dbg_file, "w", encoding="utf-8") as _dbg_f:
                            json.dump(_snapshot, _dbg_f, ensure_ascii=False, indent=2)
                        log_event(f"[CONTENT-POLICY] Снимок контекста для разбора: {_dbg_file}")
                    except Exception as _dbg_err:
                        log_event(f"[!] Не удалось сохранить снимок контекста: {_dbg_err}")
                    _removed = 0
                    try:
                        _start = locals().get("turn_start_index")
                        if isinstance(chat_history, list) and isinstance(_start, int) and _start >= 0:
                            while len(chat_history) > _start:
                                chat_history.pop()
                                _removed += 1
                            if not _session_deleted:
                                _persist_history()
                    except Exception as _rb:
                        log_event(f"[!] Не удалось откатить ход после content-policy: {_rb}")
                    final_reply = (
                        "⚠️ Провайдер отклонил запрос из-за политики контента "
                        "(Content policy violation).\n\n"
                        "Это ограничение на стороне API-провайдера, а не приложения, "
                        "и оно срабатывает на содержимое конкретного сообщения. "
                        "Вызвавшее отказ сообщение удалено из истории чата — можно "
                        "просто продолжить беседу с новой реплики.\n\n"
                        "Если отказ повторяется на нейтральных вопросах — смените модель "
                        "или провайдера в настройках профиля."
                    )
                    log_event(
                        f"[CONTENT-POLICY] Ход откачен из истории ({_removed} сообщ.), "
                        f"сессия '{session_id}'"
                    )

            # --- Гарантированная доставка ответа фронтенду ---
            # Любой сбой ниже раньше обрывал соединение без тела ответа,
            # и финальный ответ ассистента молча терялся на стороне интерфейса.
            try:
                if not isinstance(final_reply, str) or not final_reply.strip():
                    final_reply = (
                        "Агент завершил работу, но текстовый ответ не был сформирован. "
                        "Подробности — в панели логов."
                    )
                thinking_steps = [str(s) for s in (thinking_steps or [])]
                new_balance = get_live_balance(api_key, api_base)
                current_chat_chars = sum(
                    len(str(m.get("content", "")))
                    for m in chat_history
                    if isinstance(m, dict)
                )
                response_payload = {
                    "reply": final_reply,
                    "thinking": thinking_steps,
                    "reasoning": reasoning_acc or "",
                    "balance": new_balance,
                    "used_tokens": current_chat_chars // 4,
                    "cancelled": cancel_event.is_set(),
                }
                if assistant_notes:
                    response_payload["notes"] = assistant_notes
                    # для совместимости со старым фронтом — первая заметка отдельно
                    response_payload["note"] = assistant_notes[0]
            except Exception as payload_error:
                log_event(f"[!] Ошибка сборки ответа /api/chat: {payload_error}")
                response_payload = {
                    "reply": final_reply
                    if isinstance(final_reply, str) and final_reply.strip()
                    else f"Ошибка обращения к API: {payload_error}",
                    "thinking": thinking_steps if isinstance(thinking_steps, list) else [],
                    "reasoning": reasoning_acc if isinstance(reasoning_acc, str) else "",
                    "balance": 0,
                    "used_tokens": 0,
                }

            self._set_cors()
            try:
                body_bytes = json.dumps(response_payload, ensure_ascii=False).encode(
                    "utf-8", errors="replace"
                )
            except Exception as dump_error:
                log_event(f"[!] Ошибка сериализации ответа /api/chat: {dump_error}")
                body_bytes = json.dumps(
                    {
                        "reply": final_reply if isinstance(final_reply, str) else str(final_reply),
                        "thinking": [],
                        "balance": 0,
                        "used_tokens": 0,
                    },
                    ensure_ascii=False,
                ).encode("utf-8", errors="replace")
            self.wfile.write(body_bytes)
            # Ответ отправлен — живой прогресс гасим: финальный список шагов
            # уже уехал в теле ответа (thinking[]), постоянная плашка — в сообщении.
            LIVE_THINKING.pop(session_id, None)
            LIVE_REASONING.pop(session_id, None)
            CANCEL_EVENTS.pop(session_id, None)
        elif self.path == "/api/session/activate":
            # Запоминает последний открытый чат — /api/init вернёт его после рестарта.
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
                if not sid:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": "session_id required"}).encode("utf-8"))
                    return
                if sid in DELETED_SESSIONS:
                    self._set_cors()
                    self.wfile.write(json.dumps({"ok": True, "deleted": True}).encode("utf-8"))
                    return
                cfg = load_config()
                cfg["last_active_session"] = sid
                save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True, "session_id": sid}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/notes":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                notes = req_data.get("notes")
                if not isinstance(notes, list):
                    raise ValueError("notes must be an array")
                save_notes(notes)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            except Exception as e:
                self._set_cors(400)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/session/upsert":

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                sid = str(req_data.get("id") or req_data.get("session_id") or "").strip()
                if not sid:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": "id required"}).encode("utf-8"))
                    return
                if sid in DELETED_SESSIONS:
                    # Чат удалён в этом запуске: устаревший клиент не должен
                    # воскресить его повторным upsert.
                    self._set_cors()
                    self.wfile.write(json.dumps({"ok": True, "deleted": True}).encode("utf-8"))
                    return
                try:
                    _meta = load_sessions_meta()
                except Exception:
                    _meta = {}
                if not isinstance(_meta, dict):
                    _meta = {}
                cur = _meta.get(sid) or {}
                pid = str(req_data.get("personaId") or req_data.get("persona_id") or cur.get("personaId") or "").strip()
                profiles_now = load_config().get("profiles", {})
                if not pid:
                    pid = load_config().get("active_profile", "main")
                if pid not in profiles_now:
                    # Чат не может быть привязан к несуществующему профилю:
                    # раньше сюда уезжали демо-id фронтенда ("master" и т.п.),
                    # и чат «исчезал» из сайдбара после перезапуска.
                    log_event(f"[UPSERT] Чат '{sid}': профиль '{pid}' не существует, привязываю к активному")
                    pid = load_config().get("active_profile", "main")
                title = (req_data.get("title") or cur.get("title") or "Новый чат").strip() or "Новый чат"
                _meta[sid] = {
                    "personaId": pid,
                    "title": title,
                    "timestamp": req_data.get("timestamp") or cur.get("timestamp") or datetime.now().strftime("%d.%m %H:%M"),
                }
                sessions_meta_memory[sid] = _meta[sid]
                save_sessions_meta(_meta)
                # гарантируем, что сессия есть и в памяти сообщений
                if sid not in sessions_memory:
                    hd = load_history()
                    sessions_memory[sid] = hd.get(sid, [])
                    save_history(sessions_memory)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True, "session": {"id": sid, **_meta[sid]}}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/session/delete":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
                if not sid:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": "session_id required"}).encode("utf-8"))
                    return
                # 1) метка удаления: блокирует поздние записи и «воскрешение»
                #    чата при сохранении других сессий или позднем /api/chat.
                DELETED_SESSIONS.add(sid)
                # удалённый чат не должен остаться «последним открытым» —
                # иначе после рестарта приложение откроет несуществующий чат
                try:
                    cfg = load_config()
                    if cfg.get("last_active_session") == sid:
                        cfg["last_active_session"] = ""
                        save_config(cfg)
                except Exception:
                    pass
                # 2) кэш в оперативной памяти сервера
                sessions_memory.pop(sid, None)
                sessions_meta_memory.pop(sid, None)
                # 3) ФИЗИЧЕСКОЕ удаление с диска: файл истории чата, легаси-каталог,
                #    общий sessions_history.json и легаси sessions_meta.json.
                try:
                    for item in purge_session_storage(sid):
                        log_event(f"[DELETE] Стёрто: {item}")
                except Exception as purge_error:
                    log_event(f"[!] Не удалось удалить файлы чата '{sid}': {purge_error}")
                # 4) мета сессии в конфиге
                try:
                    _meta = load_sessions_meta()
                except Exception:
                    _meta = {}
                if isinstance(_meta, dict) and sid in _meta:
                    del _meta[sid]
                    save_sessions_meta(_meta)
                # 5) пересобираем памятную историю без удалённого чата
                try:
                    save_history(sessions_memory)
                except Exception as save_error:
                    log_event(f"[!] Не удалось пересохранить историю: {save_error}")
                log_event(f"[DELETE] Чат '{sid}' удалён намертво (память + диск + мета)")
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True, "session_id": sid}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/session/clear":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                sid = str(req_data.get("session_id") or req_data.get("id") or "default").strip()
                if sid in DELETED_SESSIONS or sid not in sessions_memory:
                    # Удалённую/отсутствующую сессию не «воскрешаем» пустым файлом.
                    self._set_cors()
                    self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
                    return
                # оставляем system-промпт, чистим только диалог
                kept = []
                if sessions_memory[sid] and isinstance(sessions_memory[sid][0], dict) and sessions_memory[sid][0].get("role") == "system":
                    kept = sessions_memory[sid][:1]
                sessions_memory[sid] = kept
                save_history(sessions_memory)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/save_personas":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                persona = req_data.get("persona")
                if not persona or not persona.get("id"):
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": "persona.id required"}).encode("utf-8"))
                    return
                cfg = load_config()
                profiles = cfg.get("profiles") or {}
                profile_name = persona["id"]
                existing = profiles.get(profile_name, {})
                # Профиль студии редактируется только из студии — основные профили
                # не должны его перезаписывать.
                if _is_studio_profile(existing):
                    self._set_cors(400)
                    self.wfile.write(
                        json.dumps({"error": "Это профиль документ-студии"}).encode("utf-8")
                    )
                    return
                existing.update({
                    # отображаемые поля профиля: имя, эмодзи, описание
                    "display_name": str(persona.get("name", existing.get("display_name", "")) or "").strip()
                    or profile_name.upper(),
                    "emoji": str(persona.get("emoji", existing.get("emoji", "🤖")) or "🤖"),
                    "description": str(persona.get("description", existing.get("description", ""))),
                    "base_url": persona.get("baseUrl", existing.get("base_url", "")),
                    "model": persona.get("modelName", existing.get("model", "")),
                    "api_key": retained_secret(persona.get("apiKey"), existing.get("api_key"), persona.get("clearApiKey")),
                    "system_prompt": persona.get("systemPrompt", existing.get("system_prompt", "")),
                    "temperature": float(persona.get("temperature", existing.get("temperature", 0.7))),
                    "max_steps": max(1, min(200, int(persona.get("maxSteps", existing.get("max_steps", 60)) or 60))),
                    "thinking_budget": int(persona.get("thinkingBudget", existing.get("thinking_budget", 0))),
                    "attach_max_images": max(1, int(persona.get("attachMaxImages", existing.get("attach_max_images", 4)) or 4)),
                    "attach_max_text_chars": max(100, int(persona.get("attachMaxTextChars", existing.get("attach_max_text_chars", 60000)) or 60000)),
                    "attach_max_text_per_file": max(100, int(persona.get("attachMaxTextPerFile", existing.get("attach_max_text_per_file", 60000)) or 60000)),
                    "attach_max_total_text": max(100, int(persona.get("attachMaxTotalText", existing.get("attach_max_total_text", 180000)) or 180000)),
                    "attach_csv_max_rows": max(1, int(persona.get("attachCsvMaxRows", existing.get("attach_csv_max_rows", 400)) or 400)),
                    "attach_xlsx_max_rows": max(1, int(persona.get("attachXlsxMaxRows", existing.get("attach_xlsx_max_rows", 400)) or 400)),
                    "chat_msg_max_chars": max(0, int(persona.get("chatMsgMaxChars", existing.get("chat_msg_max_chars", 0)) or 0)),
                    "chat_msg_text_part_max_chars": max(0, int(persona.get("chatMsgTextPartMaxChars", existing.get("chat_msg_text_part_max_chars", 0)) or 0)),
                })
                profiles[profile_name] = existing
                if persona.get("clearApiKey") is True:
                    existing["api_key_env"] = ""
                cfg["profiles"] = profiles
                save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path == "/api/delete_persona":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                profile_id = str(req_data.get("profile_id", "")).strip()
                cfg = load_config()
                profiles = cfg.get("profiles") or {}
                if not profile_id or profile_id not in profiles:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": "profile_id required"}).encode("utf-8"))
                    return
                # Профили студии удаляются из студии, не из основного окна.
                if _is_studio_profile(profiles.get(profile_id)):
                    self._set_cors(400)
                    self.wfile.write(
                        json.dumps({"error": "Профиль документ-студии удаляется в студии"}).encode("utf-8")
                    )
                    return
                main_profiles = {
                    name: data for name, data in profiles.items() if not _is_studio_profile(data)
                }
                if len(main_profiles) <= 1:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": "Нельзя удалить единственный профиль"}).encode("utf-8"))
                    return
                del profiles[profile_id]
                cfg["profiles"] = profiles
                if cfg.get("active_profile") == profile_id:
                    cfg["active_profile"] = next(
                        (name for name in profiles if not _is_studio_profile(profiles[name])),
                        cfg.get("active_profile"),
                    )
                save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True, "active_profile": cfg.get("active_profile")}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path == "/api/set_active_profile":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                profile_id = req_data.get("profile_id", "")
                cfg = load_config()
                profiles = cfg.get("profiles") or {}
                # Основной чат не может сделать активным профиль студии.
                if profile_id in profiles and not _is_studio_profile(profiles.get(profile_id)):
                    cfg["active_profile"] = profile_id
                    save_config(cfg)
                self._set_cors()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            except Exception as e:
                self._set_cors(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path == "/api/word/compile":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос компиляции")
                payload = _word_compile_payload(req_data)
                self._set_cors()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )

        elif self.path in (
            "/api/word/analyze",
            "/api/word/preview",
            "/api/word/apply",
            "/api/word/preset-save",
            "/api/word/state",
            "/api/word/close",
        ):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            handler = {
                "/api/word/analyze": _word_analyze_payload,
                "/api/word/preview": _word_preview_payload,
                "/api/word/apply": _word_apply_payload,
                # Явное извлечение пресета (автосоздание при анализе отключено),
                # восстановление состояния после перехода между разделами
                # и явное закрытие документа.
                "/api/word/preset-save": _word_preset_save_payload,
                "/api/word/state": _word_state_payload,
                "/api/word/close": _word_close_payload,
            }[self.path]
            try:
                req_data = json.loads(body) if body else {}
                if not isinstance(req_data, dict):
                    raise ValueError("Некорректный запрос")
                payload = handler(req_data)
                self._set_cors()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                log_event(f"[!] {self.path}: {exc}")
                self._set_cors(400)
                self.wfile.write(
                    json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                )

        else:
            self._set_cors(404)


def wait_for_server(host: str, port: int, timeout: int = 60) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
    return False


# Сигнализирует, что мост реально занял порт 5000. Без него второй экземпляр
# приложения тихо вешал свой поток и два моста начинали перехватывать запросы
# друг у друга (СO_REUSEADDR допускает двойную привязку в Windows).
BRIDGE_STARTED = threading.Event()
SHUTTING_DOWN = threading.Event()
_backend_server = None
_backend_lock = threading.Lock()
_frontend_lock = threading.Lock()
_cleanup_lock = threading.Lock()


def _write_startup_error(message: str) -> None:
    """Последняя ошибка старта — в файл рядом с конфигом: pythonw молчит,
    а без этого файла невидимый краш нельзя было диагностировать."""
    try:
        from config_manager import CONFIG_DIR

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / "last_error.log"
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        path.write_text(f"[{stamp}] {message}\n", encoding="utf-8")
    except Exception:
        pass


def start_backend():
    global _backend_server
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), BridgeHandler)
    except OSError as e:
        BRIDGE_STARTED.set()
        _write_startup_error(
            f"Порт {BRIDGE_PORT} уже занят: {e}. Скорее всего, уже работает "
            "другой экземпляр приложения — закройте его (трей → Выход) и запустите заново."
        )
        return
    with _backend_lock:
        if SHUTTING_DOWN.is_set():
            server.server_close()
            return
        _backend_server = server
    log_event(f"[*] Python Bridge запущен на порту {BRIDGE_PORT}")
    BRIDGE_STARTED.set()
    server.serve_forever()


frontend_proc = None


def start_frontend():
    global frontend_proc
    # CREATE_NO_WINDOW = 0x08000000 гарантирует отсутствие черных окон консоли
    creationflags = 0x08000000 if os.name == "nt" else 0
    # Продакшн-сборка стартует заметно быстрее dev-сервера: `next start` поднимается
    # ~за 1 с и отдаёт уже скомпилированные страницы, без компиляции на лету (именно
    # она в dev давала задержку на первом GET). Dev-режим можно принудительно вернуть
    # переменной окружения KID_FRONTEND_DEV=1 — например, во время правок интерфейса
    # (после правок фронтенда нужен `npm run build`, чтобы продакшн-сборка обновилась).
    use_prod = (
        not os.environ.get("KID_FRONTEND_DEV")
        and os.path.isfile(os.path.join(FRONTEND_DIR, ".next", "BUILD_ID"))
    )
    script = "start" if use_prod else "dev"
    log_event(f"[*] Запуск фронтенда: next {script} (порт {PORT})")
    try:
        # pythonw (GUI-подсистема) не передаёт корректно PATH в cmd.exe, поэтому
        # `npm.cmd run ... shell=True` из pythonw падает с «npm.cmd не распознан».
        # Запускаем Next.js напрямую через node.exe (абсолютный путь, без оболочки).
        node_exe = shutil.which("node.exe")
        next_bin = os.path.join(FRONTEND_DIR, "node_modules", "next", "dist", "bin", "next")
        if not node_exe or not os.path.isfile(next_bin):
            raise RuntimeError(f"node.exe или Next.js CLI не найдены (node={node_exe!r})")
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _frontend_lock:
            if SHUTTING_DOWN.is_set():
                return
            with open(USER_DATA_DIR / "frontend.log", "a", encoding="utf-8", errors="replace") as frontend_log:
                frontend_proc = subprocess.Popen(
                    [node_exe, next_bin, script, "-p", str(PORT), "--hostname", "127.0.0.1"],
                    env={**os.environ, "KID_BRIDGE_TOKEN": BRIDGE_TOKEN},
                    cwd=FRONTEND_DIR,
                    creationflags=creationflags,
                    stdout=frontend_log,
                    stderr=frontend_log,
                )
    except Exception as exc:
        log_event(f"[!] Не удалось запустить фронтенд: {exc}")
        frontend_proc = None


def shutdown_resources() -> bool:
    """Однократная очистка при закрытии окна, выходе из трея и ошибке старта."""
    global frontend_proc, _backend_server
    if not _cleanup_lock.acquire(blocking=False):
        return False
    try:
        if SHUTTING_DOWN.is_set():
            return False
        SHUTTING_DOWN.set()
        log_event("[*] Завершение приложения: остановка запросов и дочерних процессов")
        for cancel_event in list(CANCEL_EVENTS.values()):
            cancel_event.set()
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        with _backend_lock:
            server, _backend_server = _backend_server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:
                log_event(f"[!] Остановка HTTP-моста: {exc}")
            try:
                server.server_close()
            except Exception as exc:
                log_event(f"[!] Закрытие HTTP-сокета: {exc}")
        network_module = sys.modules.get("network_agent.core")
        agent = getattr(network_module, "agent_core", None)
        if agent is not None:
            try:
                agent.close()
            except Exception as exc:
                log_event(f"[!] Остановка браузера поиска: {exc}")
        with _frontend_lock:
            process, frontend_proc = frontend_proc, None
        if process is not None:
            try:
                if process.poll() is None:
                    if os.name == "nt":
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                capture_output=True, timeout=5, creationflags=0x08000000,
                            )
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            log_event(f"[!] Завершение дерева Next.js: {exc}")
                    else:
                        process.terminate()
                    if process.poll() is None:
                        process.kill()
                process.wait(timeout=5)
            except Exception as exc:
                log_event(f"[!] Остановка Next.js: {exc}")
        return True
    finally:
        _cleanup_lock.release()


def main(window_title: str = "Assistant", console: bool = True) -> None:
    from process_cleanup import retain_child_process_job

    # Job охватывает Next.js, Playwright и WebView2, даже при аварийном выходе.
    retain_child_process_job()
    try:
        _run_application(window_title=window_title, console=console)
    finally:
        shutdown_resources()


def _run_application(window_title: str = "Assistant", console: bool = True) -> None:
    """Точка входа приложения: бэкенд-мост + Next.js + WebView-окно.

    Используется и desktop_app.py, и WEB_UI.pyw (автозапуск/pythonw), чтобы
    оба лаунчера работали с одинаковым API и хранилищем.
    console=False — режим pythonw: консоли нет, input() недоступен.
    """
    startup_started = time.time()

    t_back = threading.Thread(target=start_backend, daemon=True)
    t_back.start()
    # Мост обязан занять порт: иначе это второй экземпляр — выходим с внятной
    # записью в last_error.log вместо молчаливого «хаоса двух мостов».
    if not BRIDGE_STARTED.wait(timeout=5):
        _write_startup_error("Мост не смог занять порт 5000 за 5 с.")
        log_event("[!] Мост не занял порт 5000 — выход")
        raise SystemExit(1)
    if _backend_server is None:
        raise SystemExit(1)

    # Прогрев памяти профиля в фоне: локальная модель эмбеддингов и таблица
    # LanceDB поднимаются до первого сообщения, чтобы retrieval не задерживал
    # первый ответ. Поток daemon — старт окна не блокирует.
    def _warm_profile_memory():
        if user_profile_memory is None:
            log_event(
                "[!] Модуль памяти профиля не импортирован — память выключена"
                + (f": {MEMORY_IMPORT_ERROR}" if MEMORY_IMPORT_ERROR else "")
            )
            return
        try:
            user_profile_memory.set_log_callback(log_event)
            backend = user_profile_memory.warmup()
            info = user_profile_memory.status()
            log_event(
                f"[MEMORY] Профиль готов: эмбеддинги='{backend}', "
                f"хранилище='{info.get('storage')}', фактов={info.get('facts')}, "
                f"база={info.get('db_dir')}"
            )
            log_event(f"[MEMORY] Интерпретатор памяти профиля: {info.get('python')}")
            if info.get("degraded"):
                # Память работает на запасном JSON-хранилище: причины и команда
                # починки должны быть видны в логе, а не только в догадках.
                log_event(
                    "[!] Память профиля БЕЗ LanceDB (запасное JSON-хранилище): "
                    f"{info.get('last_error') or 'lancedb не установлен'}. "
                    f"Установка: {info.get('install_hint')}"
                )
        except Exception as memory_error:
            log_event(f"[!] Прогрев памяти профиля не удался (не критично): {memory_error}")

    threading.Thread(target=_warm_profile_memory, daemon=True).start()

    if wait_for_server(HOST, PORT, timeout=1):
        _write_startup_error("Port 3000 is already in use. Close the previous frontend before starting.")
        raise SystemExit(1)
    t_front = threading.Thread(target=start_frontend, daemon=True)
    t_front.start()

    log_event("[*] Ожидание компиляции интерфейса Next.js...")
    ready = wait_for_server(HOST, PORT, timeout=45)

    if not ready:
        log_event("[!] Ошибка: Сервер не запустился вовремя. Проверьте папку проекта.")
        _write_startup_error("Next.js не поднялся на порту 3000 за 45 с.")
        if console:
            try:
                input("Нажмите Enter для выхода...")
            except (EOFError, OSError):
                pass
        raise SystemExit(1)

    # Прогрев: TCP-готовность ещё не значит, что страница скомпилирована.
    # Первый GET / запускает компиляцию ДО открытия окна — пользователь
    # видит готовый интерфейс сразу, а не пустое окно на 5-30 с.
    try:
        warm_started = time.time()
        # напрямую (localhost через системный прокси не гоняем)
        warm_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with warm_opener.open(f"http://{HOST}:{PORT}/", timeout=90) as warm_resp:
            warm_resp.read()
        log_event(f"[+] Интерфейс прогрет за {time.time() - warm_started:.1f} с")
    except Exception as warm_error:
        log_event(f"[!] Прогрев интерфейса не удался (не критично): {warm_error}")

    log_event(f"[+] Запуск завершён за {time.time() - startup_started:.1f} с. Открываем окно...")

    window = webview.create_window(
        title=window_title,
        url=f"http://{HOST}:{PORT}",
        width=1240,
        height=860,
        resizable=True,
        text_select=True,  # РАЗРЕШАЕТ ВЫДЕЛЕНИЕ И КОПИРОВАНИЕ ТЕКСТА
        background_color="#0a0a0c",
    )

    is_visible = True
    tray_icon = None

    def terminate_all(*_args):
        nonlocal tray_icon
        if not shutdown_resources():
            return
        try:
            if tray_icon:
                tray_icon.stop()
        finally:
            # ОС закрывает Job handle и дочищает всё дерево, включая WebView2.
            os._exit(0)

    def toggle_window():
        nonlocal is_visible
        if is_visible:
            window.hide()
            is_visible = False
        else:
            window.show()
            window.restore()
            is_visible = True

    def create_tray_image():
        img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, 56, 56), fill="#6366f1")
        draw.ellipse((22, 22, 42, 42), fill="#ffffff")
        return img

    def setup_tray():
        nonlocal tray_icon
        menu = pystray.Menu(
            pystray.MenuItem("Показать / Скрыть", toggle_window, default=True),
            pystray.MenuItem("Выход", terminate_all),
        )
        tray_icon = pystray.Icon(
            "sprut_assistant", create_tray_image(), "Sprut AI Assistant", menu
        )
        tray_icon.run()

    # Запуск трея в фоновом потоке
    threading.Thread(target=setup_tray, daemon=True).start()

    # Регистрация хоткея
    keyboard.add_hotkey("ctrl+shift+space", toggle_window)

    # Сворачивание прямо в трей
    def on_minimized():
        nonlocal is_visible
        window.hide()
        is_visible = False

    window.events.minimized += on_minimized
    window.events.closed += terminate_all

    webview.start()
    terminate_all()


if __name__ == "__main__":
    # Невидимый краш при старте (pythonw) должен оставлять след в last_error.log
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        _write_startup_error(traceback.format_exc())
        raise
