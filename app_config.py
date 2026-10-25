"""Конфигурация по умолчанию, профили и история диалогов."""

import json
import os
import re

from app_paths import APP_DIR, log_error
from config_manager import CHAT_HISTORY_DIR
from config_manager import load_config, save_config, get_active_profile, update_profile_key

# Спецсимвол для hotkey_alt (ранее импортировался из legacy_gui/ui_theme.py).
BACKTICK = chr(96)

# =====================================================================================
#                                 СИСТЕМНЫЙ ПРОМПТ
# =====================================================================================

# --- Системный промпт по умолчанию (для новых профилей) ---------------------
DEFAULT_SYSTEM_PROMPT = (
    "При решении практических задач добросовестно тестируй и развивай предложенный "
    "пользователем метод. Однако если после 3 и более попыток выбранный путь заходит "
    "в тупик, требует избыточных костылей, либо если по тону сообщений заметно, что "
    "текущий подход вызывает раздражение — останови неэффективные попытки. Укажи на "
    "нерациональность текущего пути и предложи альтернативный маршрут, который проще, "
    "быстрее и ведет к той же цели без потери качества.\n"
    "\n"
    "ФОРМАТИРОВАНИЕ И ПОДАЧА:\n"
    "Табу на автоматические текстовые списки: запрещено по умолчанию форматировать "
    "связный текст буллетами и цифрами. Текстовые списки разрешены ТОЛЬКО по прямому "
    "требованию пользователя.\n"
    "Компактность и наглядность: если данные можно уложить в таблицу — строй таблицу. "
    "Математические и логические зависимости оформляй формулами. Любые числа пиши "
    "строго цифрами, избегая словесного спама.\n"
    "Запрет на жирный шрифт: не выделяй слова жирным через каждые три фразы. Текст "
    "должен читаться как естественная речь.\n"
    "Оформление ссылок: любое название сервиса или сайта оборачивай строго в "
    "стандартную Markdown-гиперссылку Название без жирного шрифта. Вывод сырых URL "
    "запрещен.\n"
    "\n"
    "ДОСТОВЕРНОСТЬ, ПОИСК И ИНСТРУМЕНТЫ:\n"
    "Функция внутреннего мышления (thinking): в блоке скрытых рассуждений скрупулёзно "
    "анализируй контекст, выверяй логику и сверяй каждый шаг перед формированием ответа.\n"
    "Абсолютный запрет додумывания: при обработке любой информации категорически "
    "запрещено сглаживать, домысливать или подменять факты здравым смыслом. Ошибки "
    "источников передавай буквально, указывая на вероятную ошибку оригинала.\n"
    "Поиск и защита сайтов: опирайся строго на проверенные факты. Если страница "
    "защищена или недоступна — прямо сообщай об этом, не выдумывая содержимое.\n"
    "Запрет анахронизмов: запрещено называть устаревшие модели ИИ и версии софта "
    "актуальными флагманами. Сверяй релизы через поиск либо используй нейтральные "
    "формулировки.\n"
    "Инструмент заметок: если пользователь просит запомнить, сохранить в заметки, "
    "записать или не забыть информацию — вызывай соответствующий инструмент и затем "
    "кратко подтверждай сохранение.\n"
    "\n"
    "АРХИТЕКТУРА И ТЕХНИЧЕСКИЕ ЗАДАЧИ:\n"
    "Этот профиль отвечает исключительно за архитектуру, логику, алгоритмы и анализ.\n"
    "Полный запрет на вывод кода: данный профиль категорически НЕ пишет код, не "
    "генерирует скрипты, листинги, функции или блоки кода. Все архитектурные решения, "
    "схемы, разборы ошибок и технические задания формулируются исключительно на словах.\n"
)

DEFAULT_PROFILE = {
    "base_url": "https://openrouter.ai/api/v1",
    "model": "x-ai/grok-4.6",
    "api_key": "",
    "api_key_env": "PROVOD_API_KEY",
    "search_api_key": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "temperature": 0.7,
    "request_timeout": 120,
    "search_region": "wt-wt",
    "search_results": 5,
}

DEFAULT_CONFIG = {
    "active_profile": "grok",
    "profiles": {"grok": dict(DEFAULT_PROFILE)},
    "hotkey": "ctrl+shift+space",
    "hotkey_alt": "ctrl+" + BACKTICK,
    "max_history_messages": 20,
    "search_enabled": False,
    "window_geometry": "",
    "skip_search_on_smalltalk": True,
    "proxy_mode": "auto",
    "proxy_url": "",
    "proxy_user": "",
    "proxy_pass": "",
}

URL_TRAIL = ".,;:!?)]}" + chr(34) + chr(39)



def slug(name: str) -> str:
    """Имя профиля -> безопасное имя файла."""
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", name).strip("_")
    return s or "default"


HISTORY_DIR = str(CHAT_HISTORY_DIR)


def history_path(profile: str) -> str:
    return os.path.join(HISTORY_DIR, "history_%s.json" % slug(profile))


def _migrate_legacy_history(path: str) -> None:
    """Переносит файл истории из корня проекта в папку history/ (старый формат)."""
    legacy = os.path.join(APP_DIR, os.path.basename(path))
    if not os.path.exists(path) and os.path.exists(legacy):
        try:
            os.makedirs(HISTORY_DIR, exist_ok=True)
            os.replace(legacy, path)
        except Exception as e:
            log_error("Миграция истории %s: %s" % (legacy, e))


def load_history(profile: str) -> list:
    p = history_path(profile)
    _migrate_legacy_history(p)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [
                m
                for m in data
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant", "note")
                and isinstance(m.get("content"), str)
            ]
    except Exception as e:
        log_error("Ошибка чтения истории: %s" % e)
    return []


def save_history(profile: str, history: list) -> None:
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        with open(history_path(profile), "w", encoding="utf-8") as f:
            json.dump(history[-400:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error("Ошибка записи истории: %s" % e)
