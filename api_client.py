"""Клиент чат-API: схема инструментов, вызов completions, разбор ответа."""

import json
import re
import sys
import time

# Спецсимволы для разметки блоков <think> в ответах модели
# (ранее импортировались из legacy_gui/ui_theme.py).
LT = chr(60)  # <
GT = chr(62)  # >

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "local_search",
            "description": "Поиск по локальным документам, заметкам и истории диалогов.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Запрос"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Исследование в интернете: несколько раундов поиска, уточнение запросов, переход по страницам выдачи и чтение источников в пределах бюджета. Возвращает ссылки, фрагменты и ограничения поиска.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Запрос"},
                    "objective": {"type": "string", "description": "Что именно требуется выяснить: объект, конкретные вопросы, важные ограничения из диалога. Краткое самостоятельное задание исследователю."},
                    "sources": {"type": "array", "description": "До 4 полезных направлений; общий веб ищется всегда. Не перечисляй все. Пустой список — только веб. Если опущено, направления выбираются по запросу.",
                                "maxItems": 4, "items": {"type": "string", "enum": ["articles", "forums", "youtube", "telegram", "vk", "tiktok", "reviews", "technical", "apps", "pikabu"]}}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "Прочитать текст страницы по URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL страницы",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_github_repo",
            "description": (
                "Проанализировать GitHub-репозиторий по URL: README, структура, зависимости. "
                "Выдать справку: назначение, стек, легаси, вердикт."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Ссылка на репозиторий GitHub (https://github.com/owner/repo)",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_downloadable_file",
            "description": "Создать файл для скачивания. В content только содержимое файла.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Имя файла",
                    },
                    "content": {
                        "type": "string",
                        "description": "Содержимое файла",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "Назначение",
                    },
                    "file_format": {
                        "type": "string",
                        "enum": ["docx", "py", "txt"],
                        "description": "Формат: docx, py, txt",
                    },
                    "markdown_mode": {
                        "type": "string",
                        "enum": ["auto", "keep", "strip"],
                        "description": "Обработка markdown",
                    },
                },
                "required": ["file_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Создать заметку в панели «Заметки» (НЕ память профиля). Вызывай, когда просят записать текст.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Заголовок",
                    },
                    "content": {
                        "type": "string",
                        "description": "Текст заметки",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "Показать заметки панели «Заметки».",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_user_fact",
            "description": (
                "Сохранить ПОСТОЯННЫЙ факт о пользователе (стек, предпочтения, железо, жёсткие правила). "
                "Только для сведений, актуальных через полгода. Не сохраняй текущий код, файлы, ошибки и планы."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "Утверждение от третьего лица в настоящем времени.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "tech_stack",
                            "preferences",
                            "hardware",
                            "rules",
                            "personal_context",
                        ],
                        "description": "Категория: tech_stack, preferences, hardware, rules, personal_context.",
                    },
                },
                "required": ["fact", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": (
                "Управление памятью профиля по явной просьбе. action: list — показать факты; "
                "update/delete — изменить/удалить по fact_id (сначала вызови list); clear — очистить всё."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "update", "delete", "clear"],
                        "description": "Действие над памятью профиля",
                    },
                    "fact_id": {
                        "type": "string",
                        "description": "id факта (обязателен для update/delete)",
                    },
                    "fact": {
                        "type": "string",
                        "description": "Новый текст факта (от третьего лица, настоящее время).",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "tech_stack",
                            "preferences",
                            "hardware",
                            "rules",
                            "personal_context",
                        ],
                        "description": "Категория факта (для update)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_system_prompt",
            "description": "Изменить системный промпт профиля. Передай ПОЛНЫЙ новый текст.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Полный новый текст системного промпта",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
]

# ------------------------------------------------------------------ Document Studio
# Инструменты чата профиля документов: точечное редактирование загруженного DOCX.
# Отдельная схема (не в TOOLS_SCHEMA), чтобы не засорять обычный чат: подключается
# только для сессий Document Studio (см. desktop_app: studio-режим).
WORD_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_document_state",
            "description": (
                "Показать текущее состояние документа студии: имя файла, поля "
                "страницы, число таблиц и рисунков. Вызывай перед правками, "
                "чтобы узнать, с чем работаешь."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_page_margins",
            "description": "Задать поля страницы документа (в миллиметрах).",
            "parameters": {
                "type": "object",
                "properties": {
                    "top": {"type": "number", "description": "Верхнее поле, мм"},
                    "bottom": {"type": "number", "description": "Нижнее поле, мм"},
                    "left": {"type": "number", "description": "Левое поле, мм"},
                    "right": {"type": "number", "description": "Правое поле, мм"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_paragraph_format",
            "description": (
                "Изменить параметры абзацев выбранной области: интервалы до/после, "
                "межстрочный интервал, отступ первой строки, выравнивание. Чтобы "
                "сбросить интервалы — передай нули (или используй reset_spacing)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["body", "headings", "h1", "h2", "h3", "captions", "lists", "all", "title_page", "special_sections"],
                        "description": (
                            "Область: body, headings/h1..h3, captions, lists, all, "
                            "title_page (титульный лист), special_sections (реферат/задание)"
                        ),
                    },
                    "space_before": {"type": "number", "description": "Интервал до абзаца, pt"},
                    "space_after": {"type": "number", "description": "Интервал после абзаца, pt"},
                    "line_spacing": {"type": "number", "description": "Межстрочный интервал (кратность)"},
                    "line_spacing_rule": {
                        "type": "string",
                        "enum": ["single", "one_point_five", "double", "exactly", "at_least", "multiple"],
                        "description": "Правило межстрочного интервала",
                    },
                    "first_line_indent": {"type": "number", "description": "Отступ первой строки, мм"},
                    "alignment": {
                        "type": "string",
                        "enum": ["left", "center", "right", "justify"],
                        "description": "Выравнивание",
                    },
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_font",
            "description": "Сменить шрифт/кегль/начертание текста выбранной области.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["body", "headings", "h1", "h2", "h3", "captions", "lists", "all"],
                        "description": "Область применения",
                    },
                    "font_name": {"type": "string", "description": "Гарнитура, напр. Times New Roman"},
                    "font_size": {"type": "number", "description": "Кегль, pt"},
                    "bold": {"type": "boolean", "description": "Полужирный"},
                    "italic": {"type": "boolean", "description": "Курсив"},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "format_tables",
            "description": (
                "Отформатировать все таблицы документа: выравнивание таблицы на "
                "странице, выравнивание и шрифт текста в ячейках, отступы в ячейках."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_alignment": {
                        "type": "string",
                        "enum": ["left", "center", "right"],
                        "description": "Выравнивание таблицы на странице",
                    },
                    "cell_alignment": {
                        "type": "string",
                        "enum": ["left", "center", "right", "justify"],
                        "description": "Выравнивание текста в ячейках",
                    },
                    "font_name": {"type": "string", "description": "Шрифт ячеек"},
                    "font_size": {"type": "number", "description": "Кегль ячеек, pt"},
                    "cell_margin_top": {"type": "number", "description": "Верхний отступ, мм"},
                    "cell_margin_bottom": {"type": "number", "description": "Нижний отступ, мм"},
                    "cell_margin_left": {"type": "number", "description": "Левый отступ, мм"},
                    "cell_margin_right": {"type": "number", "description": "Правый отступ, мм"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "align_figures",
            "description": "Выровнять все рисунки документа на странице.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alignment": {
                        "type": "string",
                        "enum": ["left", "center", "right"],
                        "description": "Выравнивание рисунков",
                    }
                },
                "required": ["alignment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_spacing",
            "description": "Сбросить интервалы до/после и межстрочный интервал (в 0, одинарный) для области.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["body", "headings", "captions", "lists", "all"],
                        "description": "Область сброса интервалов",
                    }
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_document_preset",
            "description": "Полностью применить сохранённый пресет оформления к документу студии.",
            "parameters": {
                "type": "object",
                "properties": {
                    "preset_name": {
                        "type": "string",
                        "description": "Имя пресета из presets/ (например имя сохранённого пресета)",
                    }
                },
                "required": ["preset_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_document_rules",
            "description": (
                "Применить произвольный список правил к документу. Каждое правило: "
                "{scope, property, value}. scope: page/body/headings/h1/h2/h3/captions/"
                "lists/all/tables/figures/title_page/special_sections/page_numbering; "
                "property: alignment, space_before, space_after, line_spacing, "
                "first_line_indent, font_name, font_size, bold, italic, table_alignment, "
                "cell_margin_left, left_indent и т.д. Для page_numbering доступны: "
                "enabled, position, alignment, start_at, show_from_page, "
                "show_on_first_page, show_on_task_page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rules": {
                        "type": "array",
                        "description": "Список правил",
                        "items": {
                            "type": "object",
                            "properties": {
                                "scope": {"type": "string"},
                                "property": {"type": "string"},
                                "value": {},
                            },
                            "required": ["scope", "property", "value"],
                        },
                    }
                },
                "required": ["rules"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document_preset",
            "description": (
                "Вернуть активный пресет оформления документа студии в виде JSON. "
                "Используй, чтобы проанализировать текущее оформление: поля, шрифты, "
                "интервалы, таблицы, подписи, рисунки и списки."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_preset",
            "description": (
                "Вернуть сохранённый пресет оформления по имени (JSON). Полезно для "
                "сравнения текущего оформления с эталоном (например имя сохранённого пресета)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preset_name": {
                        "type": "string",
                        "description": "Имя пресета из presets/ (например имя сохранённого пресета)",
                    }
                },
                "required": ["preset_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_available_presets",
            "description": "Перечислить имена всех сохранённых пресетов оформления.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ------------------------------------------------------------------ requests
try:
    import requests
except ImportError:
    import tkinter as _t
    from tkinter import messagebox as _mb

    _r = _t.Tk()
    _r.withdraw()
    _mb.showerror(
        "Раб",
        "Не установлена библиотека requests.\n\n"
        "python -m pip install requests keyboard pystray pillow ddgs faster-whisper\n\n"
        "Также для транскрибации видео нужен ffmpeg в PATH.\n\n"
        "Интерпретатор: " + sys.executable,
    )
    _r.destroy()
    sys.exit(1)

# Модульная сессия: TCP/TLS-соединение к провайдеру переиспользуется между
# вызовами. В tool-цикле запросов несколько подряд — keep-alive экономит
# рукопожатие на каждом после первого (раньше каждый вызов открывал
# соединение заново).
_session = requests.Session()
# LLM-API ходит НАПРЯМУЮ: env-прокси (HTTP_PROXY/HTTPS_PROXY от VPN-клиента) может
# быть мёртвым или требовать авторизации — тогда первый запрос виснет на
# read-timeout (до 120 с) до фолбэка на direct, и каждый ход «думает» аномально
# долго. Для российского API (sprutdock/OpenRouter) прокси не нужен.
_session.trust_env = False

# =====================================================================================
#                                   ЗАПРОС К API
# =====================================================================================


class ApiError(Exception):
    pass


class ToolCallWithoutToolsError(ApiError):
    """Модель вернула tool_call, хотя инструменты в запросе не передавались."""


#: Маркеры, по которым распознаём отказ провайдера из-за фильтра контента.
#: Фильтр живёт НА СТОРОНЕ API-провайдера (в приложении модерации контента нет):
#: провайдер отвечает HTTP 400 с телом вида {"error": {"message": "Content policy
#: violation."}}. Такое отклонение нужно отличать от сетевых сбоев, чтобы /api/chat
#: мог откатить «отравленный» ход из истории и чат не застревал навсегда.
_CONTENT_POLICY_MARKERS = (
    "content policy",
    "content_policy",
    "policy violation",
    "policy_violation",
    "moderation",
    "safety",
    "content filter",
    "content_filter",
    "contentfilter",
    "blocked",
    "refused",
)


def is_content_policy_error(err) -> bool:
    """True, если ошибка означает отклонение запроса фильтром контента провайдера."""
    text = str(err or "").lower()
    return any(marker in text for marker in _CONTENT_POLICY_MARKERS)


THINK_OPEN = LT + "think" + GT
THINK_CLOSE = LT + "/think" + GT
THINK_RE = re.compile(
    re.escape(THINK_OPEN) + r".*?" + re.escape(THINK_CLOSE), re.DOTALL | re.IGNORECASE
)
THINK_SPLIT_RE = re.compile(LT + r"/?think" + GT, re.IGNORECASE)


def strip_thinking(text: str) -> str:
    if not text:
        return text
    text = THINK_RE.sub("", text)
    if THINK_OPEN.lower() in text.lower():
        text = THINK_SPLIT_RE.split(text)[-1]
    return text.strip()


def call_chat_api(
    base_url, api_key, model, messages, temperature, timeout,
    tools=None, stream=False, on_thinking=None, on_content=None, cancel_event=None,
):
    # Стриминговый режим: потоковая генерация (stream=True). Возвращает тот же
    # формат, что и обычный вызов (str или dict с tool_calls), а reasoning-токены
    # доставляются через on_thinking. При отмене возвращает None.
    if stream:
        streamed = _stream_chat_completions(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            timeout=timeout,
            tools=tools,
            on_thinking=on_thinking,
            on_content=on_content,
            cancel_event=cancel_event,
        )
        if streamed is None:
            return None
        return streamed[0]

    if not api_key:
        raise ApiError(
            "Не задан API-ключ. Откройте «Профили» и впишите ключ "
            "или задайте переменную окружения из поля «Переменная окружения»."
        )
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "User-Agent": "Rab/2.0",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    # Подключаем инструменты при наличии
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # ТОЧНАЯ ПРОВЕРКА РАЗМЕРА ОТПРАВЛЯЕМЫХ ДАННЫХ
    raw_data = json.dumps(payload, ensure_ascii=False)
    char_count = len(raw_data)
    line_count = raw_data.count("\n")
    print(f"[ПРОВЕРКА ОТПРАВКИ] Символов: {char_count}, Строк переноса: {line_count}")

    # Сетевые сбои провайдера (зависание TLS-рукопожатия, отсутствие ответа)
    # ретраятся: попытки чередуют системный прокси и прямое подключение, так как
    # каналы отказывают независимо друг от друга. Connect-таймаут короткий (10с):
    # зависшее рукопожатие не должно съедать весь таймаут генерации.
    read_timeout = max(10, int(timeout))
    attempts = 3
    last_error = None
    resp = None
    for attempt in range(attempts):
        force_direct = attempt % 2 == 1  # 2-я попытка всегда напрямую, без прокси
        route = "напрямую" if force_direct else "через системный прокси"
        try:
            if force_direct:
                resp = _session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(10, read_timeout),
                    proxies={"http": None, "https": None},
                )
            else:
                resp = _session.post(url, headers=headers, json=payload, timeout=(10, read_timeout))
            last_error = None
            break
        except requests.exceptions.ReadTimeout as e:
            last_error = ApiError(
                "Сервер не ответил за %s с (%s)." % (read_timeout, route)
            )
            if attempt >= 1:
                # Ответ уже отправлен на генерацию — ретраим только один раз,
                # чтобы не платить за генерацию дважды.
                break
        except requests.exceptions.ConnectTimeout as e:
            last_error = ApiError(
                "Таймаут подключения к серверу (%s), попытка %d/%d." % (route, attempt + 1, attempts)
            )
        except requests.exceptions.SSLError as e:
            last_error = ApiError("Ошибка SSL (%s): %s" % (route, e))
        except requests.exceptions.ConnectionError as e:
            last_error = ApiError(
                "Нет соединения (%s). Проверьте интернет и адрес API." % route
            )
        except Exception as e:
            last_error = ApiError("Ошибка сети (%s): %s: %s" % (route, type(e).__name__, e))
        time.sleep(0.8 * (attempt + 1))

    if last_error is not None:
        raise ApiError(
            "API недоступен после %d попыток (прокси и напрямую). %s\n"
            "Проверьте VPN/прокси и доступность %s" % (attempts, last_error, base_url)
        )

    if resp.status_code in (401, 403):
        raise ApiError(
            "HTTP %d: неверный или просроченный API-ключ." % resp.status_code
        )
    if resp.status_code == 402:
        raise ApiError(
            "HTTP 402: Недостаточно средств на балансе API (Insufficient quota). Пополните баланс аккаунта."
        )
    if resp.status_code == 404:
        raise ApiError("HTTP 404: неверный адрес API или модель.")
    if resp.status_code == 429:
        raise ApiError("HTTP 429: превышен лимит запросов.")
    if resp.status_code >= 500:
        raise ApiError("Сервер вернул ошибку %d." % resp.status_code)
    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            detail = resp.json().get("error", {}).get("message") or detail
        except Exception:
            pass
        low = (detail or "").lower()
        if (
            "image" in low
            or "modal" in low
            or "vision" in low
            or "unsupported image" in low
        ):
            raise ApiError(
                "Модель или провайдер не принимает изображения.\n"
                "Выберите vision-модель (например, qwen-vl) в профиле.\n"
                "Ответ сервера: " + detail[:200]
            )
        raise ApiError("HTTP %d: %s" % (resp.status_code, detail))

    try:
        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message") or {}
    except Exception:
        raise ApiError("Некорректный ответ сервера:\n" + resp.text[:300])

    tool_calls = msg.get("tool_calls") or []

    # Если модель запросила вызов инструментов — возвращаем объект вызова.
    # Но если инструменты в запросе отключены, это служебный ответ: пытаемся
    # вытащить текст, иначе отдаём понятную ошибку вместо сырого dict в UI.
    if tool_calls:
        if tools:
            return msg
        fallback_text = (
            msg.get("content")
            or msg.get("reasoning_content")
            or choice.get("text")
            or ""
        )
        if isinstance(fallback_text, list):
            parts = []
            for p in fallback_text:
                if isinstance(p, dict):
                    t = p.get("text")
                    if t:
                        parts.append(str(t))
            fallback_text = "\n".join(parts)
        fallback_text = strip_thinking(str(fallback_text or ""))
        if fallback_text:
            return fallback_text
        raise ToolCallWithoutToolsError(
            "Модель вернула служебный tool-вызов вместо текста. "
            "Повторите запрос (или включите интернет-поиск, если он нужен)."
        )

    content = (
        msg.get("content") or msg.get("reasoning_content") or choice.get("text") or ""
    )
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if t:
                    parts.append(str(t))
        content = "\n".join(parts)
    content = strip_thinking(str(content or ""))
    if not content:
        raise ApiError("Модель вернула пустой ответ.")
    return content


def _raise_http_error(resp):
    """Общая карта HTTP-ошибок для стримингового вызова."""
    if resp.status_code in (401, 403):
        raise ApiError(
            "HTTP %d: неверный или просроченный API-ключ." % resp.status_code
        )
    if resp.status_code == 402:
        raise ApiError(
            "HTTP 402: Недостаточно средств на балансе API (Insufficient quota). Пополните баланс аккаунта."
        )
    if resp.status_code == 404:
        raise ApiError("HTTP 404: неверный адрес API или модель.")
    if resp.status_code == 429:
        raise ApiError("HTTP 429: превышен лимит запросов.")
    if resp.status_code >= 500:
        raise ApiError("Сервер вернул ошибку %d." % resp.status_code)
    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            detail = resp.json().get("error", {}).get("message") or detail
        except Exception:
            pass
        low = (detail or "").lower()
        if (
            "image" in low
            or "modal" in low
            or "vision" in low
            or "unsupported image" in low
        ):
            raise ApiError(
                "Модель или провайдер не принимает изображения.\n"
                "Выберите vision-модель (например, qwen-vl) в профиле.\n"
                "Ответ сервера: " + detail[:200]
            )
        raise ApiError("HTTP %d: %s" % (resp.status_code, detail))



def _stream_chat_completions(
    base_url, api_key, model, messages, temperature, timeout,
    tools=None, on_thinking=None, on_content=None, cancel_event=None,
):
    """Стриминговый вызов chat/completions (stream=True).

    Возвращает кортеж (result, reasoning_text):
      - result — тот же формат, что и у call_chat_api: str (текст) либо dict
        (сообщение с tool_calls и reasoning_content). Совместимо с текущими
        вызывающими кодами;
      - reasoning_text — накопленный текст «размышлений» модели (reasoning content
        / thought parts), пришедший за этот вызов (может быть пустой строкой).

    on_thinking(delta_text) вызывается по мере поступления reasoning-токенов,
    on_content(delta_text) — content-токенов. Оба опциональны и не должны бросать.
    При отмене (cancel_event установлен) возвращает None.
    """
    if not api_key:
        raise ApiError(
            "Не задан API-ключ. Откройте «Профили» и впишите ключ "
            "или задайте переменную окружения из поля «Переменная окружения»."
        )
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "User-Agent": "Rab/2.0",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # Та же retry-схема, что и в call_chat_api: попытки чередуют системный прокси
    # и прямое подключение. При стриме чтение идёт потоком, поэтому read-таймаут
    # относится к задержке между чанками, а не ко всей генерации.
    read_timeout = max(10, int(timeout))
    attempts = 3
    last_error = None
    resp = None
    for attempt in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            return None
        force_direct = attempt % 2 == 1
        route = "напрямую" if force_direct else "через системный прокси"
        try:
            if force_direct:
                resp = _session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(10, read_timeout),
                    stream=True,
                    proxies={"http": None, "https": None},
                )
            else:
                resp = _session.post(
                    url, headers=headers, json=payload, timeout=(10, read_timeout), stream=True
                )
            last_error = None
            break
        except requests.exceptions.ReadTimeout as e:
            last_error = ApiError(
                "Сервер не ответил за %s с (%s)." % (read_timeout, route)
            )
            if attempt >= 1:
                break
        except requests.exceptions.ConnectTimeout as e:
            last_error = ApiError(
                "Таймаут подключения к серверу (%s), попытка %d/%d." % (route, attempt + 1, attempts)
            )
        except requests.exceptions.SSLError as e:
            last_error = ApiError("Ошибка SSL (%s): %s" % (route, e))
        except requests.exceptions.ConnectionError as e:
            last_error = ApiError(
                "Нет соединения (%s). Проверьте интернет и адрес API." % route
            )
        except Exception as e:
            last_error = ApiError("Ошибка сети (%s): %s: %s" % (route, type(e).__name__, e))
        time.sleep(0.8 * (attempt + 1))

    if last_error is not None:
        raise ApiError(
            "API недоступен после %d попыток (прокси и напрямую). %s\n"
            "Проверьте VPN/прокси и доступность %s" % (attempts, last_error, base_url)
        )

    _raise_http_error(resp)

    content_type = (resp.headers.get("Content-Type") or "").lower()
    is_sse = "text/event-stream" in content_type

    reasoning_parts = []
    content_parts = []
    tool_calls_acc = {}

    def _emit_reasoning(text):
        if not text:
            return
        reasoning_parts.append(text)
        if on_thinking is not None:
            try:
                on_thinking(text)
            except Exception:
                pass

    def _emit_content(text):
        if not text:
            return
        content_parts.append(text)
        if on_content is not None:
            try:
                on_content(text)
            except Exception:
                pass

    def _collect(obj):
        """Достаёт reasoning и content из delta/message независимо от схемы провайдера."""
        if not isinstance(obj, dict):
            return
        for key in ("reasoning_content", "reasoning", "thinking", "thought"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                _emit_reasoning(val)
        content = obj.get("content")
        if isinstance(content, str):
            if content:
                _emit_content(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                ptext = part.get("text")
                pthink = part.get("thinking")
                preason = part.get("reasoning")
                if ptype in ("thinking", "reasoning", "thought") or (ptext is None and (pthink or preason)):
                    candidate = pthink or preason or ptext
                    if isinstance(candidate, str) and candidate:
                        _emit_reasoning(candidate)
                elif isinstance(ptext, str) and ptext:
                    _emit_content(ptext)

    try:
        if is_sse:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    return None
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                _collect(delta)
                # Некоторые провайдеры шлют первым чанком message, а не delta.
                msg = choice.get("message")
                if isinstance(msg, dict):
                    _collect(msg)
                dtc = delta.get("tool_calls") or []
                for tc in dtc:
                    if not isinstance(tc, dict):
                        continue
                    idx = int(tc.get("index", 0))
                    acc = tool_calls_acc.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        acc["id"] = str(tc["id"])
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] = str(fn["name"])
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += str(fn["arguments"])
        else:
            # Провайдер проигнорировал stream — читаем обычный JSON и разбираем его.
            try:
                data = resp.json()
                choice = data["choices"][0]
                msg = choice.get("message") or {}
            except Exception:
                raise ApiError("Некорректный ответ сервера:\n" + resp.text[:300])
            _collect(msg)
            _collect(choice)
            tool_calls = msg.get("tool_calls") or []
            for idx, tc in enumerate(tool_calls):
                if isinstance(tc, dict):
                    tool_calls_acc[idx] = {
                        "id": str(tc.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str((tc.get("function") or {}).get("name") or ""),
                            "arguments": str((tc.get("function") or {}).get("arguments") or ""),
                        },
                    }
    finally:
        try:
            resp.close()
        except Exception:
            pass

    reasoning_text = "".join(reasoning_parts)
    content_text = "".join(content_parts)

    tool_calls = [
        tool_calls_acc[idx]
        for idx in sorted(tool_calls_acc)
        if tool_calls_acc[idx]["function"]["name"] or tool_calls_acc[idx]["id"]
    ]

    if tool_calls:
        if tools:
            return (
                {
                    "role": "assistant",
                    "content": content_text,
                    "tool_calls": tool_calls,
                    "reasoning_content": reasoning_text,
                },
                reasoning_text,
            )
        fallback_text = strip_thinking(content_text or reasoning_text)
        if fallback_text:
            return (fallback_text, reasoning_text)
        raise ToolCallWithoutToolsError(
            "Модель вернула служебный tool-вызов вместо текста. "
            "Повторите запрос (или включите интернет-поиск, если он нужен)."
        )

    # <think>...</think> не вырезаем здесь: это делает вызывающий код (desktop_app),
    # чтобы размышления, вложенные моделью прямо в content, не терялись.
    content_text = content_text or ""
    if not content_text:
        if reasoning_text:
            return ("", reasoning_text)
        raise ApiError("Модель вернула пустой ответ.")
    return (content_text, reasoning_text)
