"""
Сборка и запуск десктопного приложения Windows с интерфейсом Next.js.
Компоненты и callbacks подключаются здесь; HTTP и сценарии — в application.
Старые имена функций и объектов сохранены для совместимости с callers/scripts.
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
import codex_engine
from tool_execution import execute_tool, ToolExecutionError
from code_workspace import CodeWorkspace
from codex_service import CodexService
import attach_tools
from application import file_transfer, settings_profiles, codex_actions, chat_codex, chat_api
from application.file_transfer import (
    _download_url, _content_disposition, get_desktop_dir,
    _pick_directory_native, _unique_target_path,
)
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
CODEX_LIMITS = codex_engine.LimitsCache(USER_DATA_DIR)
CODE_WORKSPACES = CodeWorkspace()
CODEX_SERVICE = CodexService(USER_DATA_DIR, CODEX_LIMITS, CODE_WORKSPACES.root)

# Кольцевой буфер последних событий бэкенда — читается фронтендом через /api/logs
LOG_BUFFER: "collections.deque[str]" = collections.deque(maxlen=500)

# Живой прогресс текущей генерации по сессиям: фронтенд опрашивает
# GET /api/progress?session_id=..., чтобы плашка «Thinking process» показывала
# вызовы инструментов в реальном времени. Регистрируется при старте /api/chat,
# снимается после отправки ответа (финальный список по-прежнему возвращается
# в теле ответа как thinking[]).
from application.chat_progress import ChatProgress
CHAT_PROGRESS = ChatProgress()
LIVE_THINKING = CHAT_PROGRESS.thinking

# Живой поток «размышлений» модели (reasoning content / thought parts):
# накапливается по мере стриминга и уходит в GET /api/progress отдельным полем
# reasoning, чтобы виджет Thinking process показывал связный текст рассуждений
# в реальном времени, пока формируется финальный ответ.
LIVE_REASONING = CHAT_PROGRESS.reasoning
LIVE_REPLY = CHAT_PROGRESS.reply

# События отмены генерации по сессиям: POST /api/stop выставляет событие,
# а циклы /api/chat (обычный и ассистент) проверяют его между шагами; долгие
# вызовы LLM перестают ожидаться (см. _call_chat_cancellable). Регистрируется
# при старте /api/chat, снимается вместе с LIVE_THINKING.
CANCEL_EVENTS = CHAT_PROGRESS.cancel_events


from application.chat_api import _GenerationStopped, _call_chat_cancellable


# Compatibility names for existing launchers and callers.
from application.chat_messages import (
    _collapse_ws, _strip_reasoning_from_reply,
    _api_safe_msg, _repair_tool_chain, _prepare_messages_for_api,
)

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
    from services.backups import create_project_backup as create_backup

    return create_backup(BASE_DIR, BACKUP_DIR, USER_DATA_DIR, log_event)


from services.document_studio import (
    DocumentStudio, WORD_TOOL_NAMES as _WORD_TOOL_NAMES, STUDIO_HINT as _STUDIO_HINT,
)

DOCUMENT_STUDIO = DocumentStudio(
    lambda reference: _uploaded_document_path(reference),
    _download_url, lambda message: log_event(message),
)
STUDIO_DOCS = DOCUMENT_STUDIO.documents
STUDIO_LOCK = DOCUMENT_STUDIO.lock


def _word_presets_payload() -> dict:
    return DOCUMENT_STUDIO.presets()


def _uploaded_document_path(reference: str):
    return file_transfer._uploaded_document_path(reference, FILE_ACCESS)


def _word_compile_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.compile(req_data)


#: Рабочие документы Document Studio: session_id -> {path, name, preset}.
#: Файл живёт на бэкенде с момента прикрепления — модель обращается к нему через
#: инструменты, не получая его как обычное вложение чата.

#: Имена агентских инструментов редактора DOCX (схема api_client.WORD_TOOLS_SCHEMA).

#: Подсказка для модели в чате профиля документов (добавляется к системному промпту).


def _studio_state(session_id: str) -> dict:
    return DOCUMENT_STUDIO.state(session_id)


def _studio_register(session_id: str, path: str='', name: str='', preset_name: str='', preset_json: object=None) -> None:
    return DOCUMENT_STUDIO.register(session_id, path, name, preset_name, preset_json)


def _studio_set_preset(session_id: str, preset_json: object) -> None:
    return DOCUMENT_STUDIO.set_preset(session_id, preset_json)


def _studio_set_presets(session_id: str, presets_json: list) -> None:
    return DOCUMENT_STUDIO.set_presets(session_id, presets_json)


def _generated_file(file_id: str):
    return file_transfer._generated_file(file_id, FILE_ACCESS)


def _studio_reference(req_data: dict) -> str:
    return DOCUMENT_STUDIO.reference(req_data)


def _word_analyze_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.analyze(req_data)


def _word_preset_save_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.save_document_preset(req_data)


def _word_state_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.document_state(req_data)


def _word_close_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.close(req_data)


def _word_preview_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.preview(req_data)


def _word_apply_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.apply(req_data)


def _run_word_tool(fn_name: str, fn_args: dict, session_id: str) -> str:
    return DOCUMENT_STUDIO.run_tool(fn_name, fn_args, session_id)


def _word_save_preset_payload(req_data: dict) -> dict:
    return DOCUMENT_STUDIO.save_preset(req_data)


HISTORY_FILE = str(USER_DATA_DIR / "sessions_history.json")
NOTES_FILE = str(USER_DATA_DIR / "notes.json")
from services.notes import NoteStore
NOTES = NoteStore(NOTES_FILE, lambda message: log_event(message))
_NOTES_LOCK = NOTES.lock
CONFIG_PATH = str(USER_DATA_DIR / "config.json")
FRONTEND_DIR = os.path.join(BASE_DIR, "UI")
HOST = "127.0.0.1"
PORT = 3000
BRIDGE_PORT = 5000


def load_history():
    return SESSION_HISTORY.load_all()


def save_history(history_data):
    return SESSION_HISTORY.save_all(history_data)


def load_notes():
    return NOTES.load()


def save_notes(notes):
    return NOTES.save(notes)


def prepend_note(note):
    return NOTES.prepend(note)


def _codex_local_tool(name, arguments, profile_id, cancel):
    return codex_actions.execute_host_tool(
        name, arguments, profile_id, cancel,
        service=CODEX_SERVICE, workspaces=CODE_WORKSPACES,
        load_config=load_config, save_config=save_config,
        read_notes=load_notes, prepend_note=prepend_note,
    )


CODEX_SERVICE.local_tools = _codex_local_tool
from native_codex.setup import ensure as _prepare_native_codex
CODEX_SERVICE.bootstrap = _prepare_native_codex


from application.session_history import SessionHistory, _meta_title_from_prompt, _msg_text

SESSION_HISTORY = SessionHistory(
    lambda: load_config(), lambda config: save_config(config),
    lambda message: log_event(message), lambda sid: CODEX_SERVICE.forget(sid),
)
sessions_memory = SESSION_HISTORY.memory
CODEX_SERVICE.history_reader = lambda session_id: list(sessions_memory.get(session_id, []))
sessions_meta_memory = SESSION_HISTORY.metadata

SESSIONS_META_FILE = str(USER_DATA_DIR / "sessions_meta.json")


def load_sessions_meta():
    return SESSION_HISTORY.read_metadata()


def save_sessions_meta(meta):
    return SESSION_HISTORY.write_metadata(meta)


# --------------------------------------------------------------------------------------
# Document Studio: отдельные профили и чаты (полностью независимы от основных)
# --------------------------------------------------------------------------------------

#: Флаг профиля, принадлежащего студии (хранится в общем config["profiles"]).
#: Имя стартового профиля студии.
#: Ключ рабочего документа студии (общий для всех её чатов; совпадает с фронтендом).

#: In-memory кэш чатов студии и их метаданных (не смешивается с основными чатами).
#: Чаты студии, удалённые в этом запуске (блокирует «воскрешение»).


from application.studio_profiles import (
    StudioSessions, STUDIO_ACTIVE_KEY, STUDIO_META_KEY, STUDIO_PROFILE_FLAG,
    STUDIO_DEFAULT_PROFILE, STUDIO_SESSION_ID,
    _is_studio_profile, _studio_persona, _studio_profile_from_persona,
)

STUDIO_SESSIONS = StudioSessions(
    lambda: load_config(), lambda config: save_config(config), lambda message: log_event(message),
)
studio_sessions_memory = STUDIO_SESSIONS.history
studio_sessions_meta_memory = STUDIO_SESSIONS.metadata
DELETED_STUDIO_SESSIONS = STUDIO_SESSIONS.deleted


def load_studio_sessions_meta() -> dict:
    return STUDIO_SESSIONS.read_metadata()


def save_studio_sessions_meta(meta: dict) -> None:
    return STUDIO_SESSIONS.write_metadata(meta)


def _make_studio_profile(base: dict | None=None) -> dict:
    return STUDIO_SESSIONS.make_profile(base)


def ensure_studio_profiles() -> None:
    return STUDIO_SESSIONS.ensure_profiles()


def _studio_profiles() -> dict:
    return STUDIO_SESSIONS.profiles()


def _studio_active_name() -> str:
    return STUDIO_SESSIONS.active_name()


def _studio_init_payload() -> dict:
    return STUDIO_SESSIONS.init_payload()


def _studio_handle(path: str, req_data: dict) -> dict:
    return STUDIO_SESSIONS.handle(path, req_data)


try:
    SESSION_HISTORY.metadata.update(load_sessions_meta())
except Exception:
    pass

# id чатов, удалённых в текущем запуске. Набор не даёт «воскреснуть» стёртой
# сессии при сохранении других данных, позднем /api/chat или повторном /api/init.
DELETED_SESSIONS = SESSION_HISTORY.deleted


# Последний успешно полученный баланс: при сбое сети/API показываем его,
# а не обнуляем витрину (иначе баланс «слетал» в 0 при каждом таймауте).
from application.provider_status import ProviderStatus

PROVIDER_STATUS = ProviderStatus(_NO_PROXY_OPENER, lambda message: log_event(message))
_LAST_KNOWN_BALANCE = PROVIDER_STATUS.last_known
_BALANCE_CACHE = PROVIDER_STATUS.cache
_BALANCE_TTL = PROVIDER_STATUS.ttl
_BALANCE_REFRESH_LOCK = PROVIDER_STATUS.refresh_lock
_BALANCE_REFRESH_STATE = PROVIDER_STATUS.refresh_state
_BALANCE_REFRESH_MIN_INTERVAL = PROVIDER_STATUS.refresh_min_interval


def get_live_balance(api_key, base_url='', force=False):
    return PROVIDER_STATUS.balance(api_key, base_url, force)


def _spawn_balance_refresh(api_key):
    return PROVIDER_STATUS.start_refresh(api_key)


def _fetch_balance(api_key):
    return PROVIDER_STATUS.fetch(api_key)


def _run_api_chat(request, req_data, session_id):
    chat_api.run_turn(
        request, req_data, session_id,
        sessions=SESSION_HISTORY, studio_sessions=STUDIO_SESSIONS,
        progress=CHAT_PROGRESS, file_access=FILE_ACCESS,
        documents=DOCUMENT_STUDIO, notes=NOTES,
        load_config=load_config, save_config=save_config,
        get_balance=get_live_balance, log_event=log_event,
        memory=user_profile_memory, memory_import_error=MEMORY_IMPORT_ERROR,
        user_data_dir=USER_DATA_DIR, call_chat=_call_chat_cancellable,
    )


from application.http_bridge import make_bridge_handler
from services.telegram_bot import TelegramBot

TELEGRAM = TelegramBot(USER_DATA_DIR, CODEX_SERVICE, lambda: load_config(), lambda message: log_event(message))
CODEX_SERVICE.telegram_export = TELEGRAM.queue_file
CODEX_SERVICE.telegram_archive = TELEGRAM.archive_tool


def _codex_history_reader(session_id):
    history = TELEGRAM.history(session_id) if str(session_id).startswith("telegram_") else None
    return history if history is not None else list(sessions_memory.get(session_id, []))


CODEX_SERVICE.history_reader = _codex_history_reader

BridgeHandler = make_bridge_handler(
    get_bridge_port=lambda: BRIDGE_PORT,
    file_access=FILE_ACCESS, user_data_dir=USER_DATA_DIR, backup_dir=BACKUP_DIR,
    sessions=SESSION_HISTORY, studio_sessions=STUDIO_SESSIONS,
    documents=DOCUMENT_STUDIO, providers=PROVIDER_STATUS, codex=CODEX_SERVICE,
    workspaces=CODE_WORKSPACES, progress=CHAT_PROGRESS, log_buffer=LOG_BUFFER,
    load_config=lambda: load_config(), save_config=lambda config: save_config(config),
    read_history=lambda: load_history(), read_notes=lambda: load_notes(),
    write_notes=lambda notes: save_notes(notes),
    get_balance=lambda *args, **kwargs: get_live_balance(*args, **kwargs),
    create_backup=lambda: create_project_backup(),
    connect_startup_codex=lambda: _connect_startup_codex(),
    pick_directory=lambda *args, **kwargs: _pick_directory_native(*args, **kwargs),
    get_desktop_dir=lambda: get_desktop_dir(),
    get_generated_file=lambda file_id: _generated_file(file_id),
    get_uploaded_document=lambda reference: _uploaded_document_path(reference),
    run_api_chat=_run_api_chat, log_event=lambda message: log_event(message), telegram=TELEGRAM,
)


# Сигнализирует, что мост реально занял порт 5000. Без него второй экземпляр
# приложения тихо вешал свой поток и два моста начинали перехватывать запросы
# друг у друга (СO_REUSEADDR допускает двойную привязку в Windows).


from application.lifecycle import (
    ApplicationLifecycle, wait_for_server, _write_startup_error, _BridgeHTTPServer,
)

LIFECYCLE = ApplicationLifecycle(FRONTEND_DIR, USER_DATA_DIR, lambda message: log_event(message))
BRIDGE_STARTED = LIFECYCLE.bridge_started
SHUTTING_DOWN = LIFECYCLE.shutting_down


def start_backend():
    return LIFECYCLE.start_backend(BridgeHandler, BRIDGE_PORT)


def start_frontend():
    return LIFECYCLE.start_frontend(PORT, BRIDGE_PORT, BRIDGE_TOKEN)


def shutdown_resources():
    from services.voice_input import close_voice_input
    close_voice_input()
    TELEGRAM.close(timeout=0)
    result = LIFECYCLE.shutdown(CODEX_SERVICE, CANCEL_EVENTS, keyboard.unhook_all_hotkeys)
    TELEGRAM.close(timeout=5)
    return result


from application.desktop_window import DesktopWindow

WINDOW = DesktopWindow(SHUTTING_DOWN, lambda: shutdown_resources())
_window_ready = WINDOW.ready


def main(window_title: str = "Assistant", console: bool = True) -> None:
    from process_cleanup import desktop_instance, retain_child_process_job

    if not desktop_instance(_activate_existing_window):
        return

    # Job охватывает Next.js, Playwright и WebView2, даже при аварийном выходе.
    retain_child_process_job()
    try:
        _run_application(window_title=window_title, console=console)
    finally:
        shutdown_resources()


def _activate_existing_window():
    return WINDOW.activate()


def _connect_startup_codex():
    """Одно фоновое подключение выбранного профиля через штатный isolation gate."""
    try:
        selected = load_config()
        profile_id = selected.get("active_profile", "main")
        profile = (selected.get("profiles") or {}).get(profile_id, {})
        if SHUTTING_DOWN.is_set() or codex_engine.engine(profile) != "codex":
            return
        log_event("[CODEX] Автоматическое подключение через прокси…")
        result = CODEX_SERVICE.action(profile_id, profile, "refresh")
        log_event("[CODEX] Защищённый канал подключён" if result.get("proxy_state") == "verified"
                  else "[CODEX] Прокси недоступен. Запрос Codex заблокирован")
    except Exception:
        # Конфигурация и исключения могут содержать секреты; не печатаем их.
        log_event("[CODEX] Автоматическое подключение недоступно. Прямой маршрут запрещён")


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
    if LIFECYCLE.backend_server is None:
        raise SystemExit(1)

    TELEGRAM.start()

    # Прогрев памяти профиля в фоне: локальная модель эмбеддингов и таблица
    # LanceDB поднимаются до первого сообщения, чтобы retrieval не задерживал
    # первый ответ. Поток daemon — старт окна не блокирует.
    def _warm_profile_memory():
        selected = load_config()
        if codex_engine.engine((selected.get("profiles") or {}).get(selected.get("active_profile", "main"), {})) == "codex":
            # FastEmbed's first warmup can download a model. Codex memory is
            # initialized only by the isolated tool executor, never on the host.
            return
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
    threading.Thread(target=_connect_startup_codex, daemon=True, name="kid-codex-startup").start()

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

    WINDOW.run(window_title, f"http://{HOST}:{PORT}")


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
