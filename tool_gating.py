"""Пре-валидация запроса: решает, какие инструменты отправлять в payload.

Зачем: слабые модели (Gemini Flash через OpenAI-совместимый прокси) при наличии
непустого массива tools вызывают функции почти на каждое сообщение. Полагаться
на ``tool_choice="auto"`` нельзя — прокси при трансляции схемы в нативный
Gemini (``tool_config.function_calling_config``) может терять или искажать
``tool_choice``. Единственный надёжный рычаг — не отправлять tools, когда они
заведомо не нужны.

Важно: модуль НЕ запрещает модели думать. Автономность сохраняется — для
содержательных запросов отправляется полный набор инструментов, и модель сама
решает, вызывать ли поиск или запоминание. Отсекаются только тривиальные
реплики («тест», «привет», «спасибо»), где вызов инструмента заведомо лишний.

Маркеры портированы из legacy_gui/chat_app.py (SMALLTALK_STARTS / wants_link),
где выборочная отправка инструментов уже работала.
"""

import math
import re
import zlib

import search_tools

# --- Маркеры small talk (порт из legacy_gui/chat_app.py:190-216) -------------
SMALLTALK_STARTS = (
    "привет", "здравств", "хай", "хеллоу", "hello", "hi ", "hi",
    "доброе утро", "добрый день", "добрый вечер", "спасибо", "благодар",
    "пока", "до свидан", "спок", "как дела", "как ты", "ок", "окей",
    "ясно", "понял", "понятно", "ага", "круто", "супер", "лол",
    "тест", "test", "ping", "ку",
)

# --- Явные маркеры потребности во внешних данных (порт из legacy:227-248) ---
RETRIEVAL_MARKERS = (
    "ссылк", "линк", "url", "http", "найди", "поищи", "загугли", "сайт",
    "источник", "статья", "новост", "курс ", "цена", "видео", "youtube",
    "ютуб", "скачать", "где посмотреть", "сегодня", "свежие", "актуальн",
    "погода", "последн", "сколько стоит", "что такое", "кто такой",
    "документация", "github", "репозитор", "пример кода",
    # модели ИИ и версии софта (защита от анахронизмов)
    "модел", "gemini", "gpt", "claude", "llm", "нейросет", "нейронк",
    "версия", "флагман", "openai", "anthropic", "deepseek", "grok",
    "mistral", "машинное обучени",
)

# --- Маркеры отсылки к реальному миру: события, новости, происшествия, даты ---
# Наличие любого из них безусловно обязывает отправить веб-поиск, даже если
# вопрос сформулирован субъективно («что думаешь», «как относишься»,
# «твоё мнение») — внешние факты не зависят от вопросительной формы.
EVENT_MARKERS = (
    # временные привязки
    "на днях", "недавно", "сегодня", "вчера", "завтра", "позавчера",
    "на этой неделе", "на прошлой неделе", "в этом месяце", "в прошлом месяце",
    "сейчас", "свеж", "последн", "новост", "актуальн", "в этом году",
    "в прошлом году",
    # события / происшествия / инциденты
    "произошло", "происшеств", "случил", "событи", "инцидент", "авари",
    "катастроф", "пожар", "наводнени", "землетрясени", "ураган", "теракт",
    "взрыв", "скандал", "погибл", "смерт", "задержан", "арест", "забастовк",
    "митинг", "выбор", "референдум", "санкци", "обстрел", "ракет",
    # организации / институции / публичные персоны в новостном контексте
    " компания", "компани", "корпорац", "организац", "правительств",
    "министерств", "власти", "чиновник", "политик", "президент", "губернатор",
    "мэр ", "депутат", "партия", "завод", "фабрик", "банк", "бирж",
    "стартап", "концерн", "холдинг", "ведомств", "служба безопасност",
    "мвд", "фсб", "минобороны", "цб ", "центробанк",
)

# Явные даты: «12 октября», «12.10.2026», «2026», «2026 год».
_DATE_RE = re.compile(
    r"\b\d{1,2}\s+(январ|феврал|март|апрел|мая|май|июн|июл|август|сентябр|октябр|ноябр|декабр)"
    r"|\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b"
    r"|\b(?:19|20)\d{2}\b"
)

# --- Маркеры действий: какие «не-поисковые» инструменты реально запрошены ----
ACTION_MARKERS = {
    "create_downloadable_file": (
        "файл", "документ", "docx", "скача", "сгенерируй файл", "pdf",
    ),
    "save_note": (
        "сохрани заметк", "запиши заметк", "добавь заметк", "создай заметк",
        "сохрани в заметки", "не забудь", "напомни",
    ),
    "list_notes": (
        "покажи заметки", "какие заметки", "что в заметках", "прочитай заметки",
        "покажи мои заметки", "список заметок", "напомни что в заметках",
    ),
    "remember_user_fact": (
        "запомни", "учти что я", "учти, что я", "я использую", "у меня",
        "моё правило", "мое правило", "мой стек", "мой проект", "мне нравится",
        "я работаю", "я пишу", "мои требования",
    ),
    "manage_memory": (
        "удали из памяти", "сотри из памяти", "очисти память", "сотри память",
        "что ты помнишь", "что помнишь", "покажи память", "покажи что помнишь",
        "что ты обо мне знаешь", "что ты знаешь обо мне", "что ты знаешь про меня",
        "что тебе известно обо мне", "что ты обо мне помнишь", "что ты про меня помнишь",
        "расскажи что ты знаешь обо мне", "расскажи что ты помнишь обо мне",
        "расскажи что обо мне знаешь", "какие факты обо мне", "какие факты ты знаешь обо мне",
        "какая у тебя память", "что у тебя в памяти", "покажи что знаешь обо мне",
        "покажи что обо мне",
        "измени факт", "измени память", "измени в памяти", "удали факт", "забудь",
        "перестань помнить", "поправь", "исправь", "замени", "поменяй", "скорректируй",
        "обнови", "уточни", "ты теперь", "я теперь", "теперь я", "уже не", "больше не",
        "поправь в памяти", "исправь в памяти", "замени в памяти", "обнови память",
        "обнови факт", "поменяй в памяти",
    ),
    "set_system_prompt": (
        "системный промпт", "системный промт", "системный пром", "системпромпт",
        "измени промпт", "поменяй промпт", "обнови промпт", "перепиши промпт",
        "измени инструкции", "поменяй инструкции", "добавь в промпт", "добавь в системный",
    ),
}

# Инструменты получения информации (внешние + локальная база)
RETRIEVAL_TOOLS = ("local_search", "web_search", "fetch_webpage")

# Порог длины для «тривиальной» реплики
TRIVIAL_MAX_CHARS = 28
# Короткое продолжение прошлого tool-хода («а подробнее?», «продолжай»)
_SHORT_FOLLOWUP_MAX_CHARS = 60
# Минимум слов, чтобы считать запрос содержательным
_SUBSTANTIVE_MIN_WORDS = 5


def normalize(text) -> str:
    """Нормализует реплику: нижний регистр, схлопнутые пробелы, без пунктуации."""
    s = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return s.strip("!?.,;:()[]\"'«»…-— ")


def _has_url(text: str) -> bool:
    """Есть ли в тексте явная ссылка."""
    return bool(re.search(r"https?://|www\.", str(text or "")))


# Ссылка на GitHub-репозиторий: github.com/owner/repo[.git] (с/без схемы) или
# git@github.com:owner/repo.git. Только для неё шлём analyze_github_repo вместо поиска.
_GITHUB_REPO_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"|git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)


def is_github_repo_url(text) -> bool:
    """Есть ли в реплике ссылка на GitHub-репозиторий."""
    return bool(_GITHUB_REPO_RE.search(str(text or "")))


def has_event_markers(text) -> bool:
    """Есть ли в реплике отсылка к реальным событиям, новостям, датам или происшествиям.

    Такие запросы требуют веб-поиска БЕЗУСЛОВНО: субъективная форма
    («что думаешь», «как относишься», «твоё мнение») не отменяет того факта,
    что речь идёт о внешних событиях реального мира.
    """
    s = normalize(text)
    if not s:
        return False
    if any(m in s for m in EVENT_MARKERS):
        return True
    return bool(_DATE_RE.search(s))


def is_trivial(text) -> bool:
    """Короткая бессодержательная реплика: «тест», «привет», «ок», «спасибо».

    Такие сообщения не требуют ни поиска, ни запоминания — инструменты для них
    не отправляются вовсе.
    """
    s = normalize(text)
    if not s:
        return True
    if _has_url(s):
        return False
    # Отсылка к реальным событиям/новостям/датам — это не small talk, даже если
    # вопрос короткий и задан субъективно.
    if has_event_markers(s):
        return False
    if len(s) > TRIVIAL_MAX_CHARS:
        return False
    if any(s.startswith(w) for w in SMALLTALK_STARTS):
        return True
    # 1-2 коротких токена без маркеров поиска/действий — тоже тривиально
    if len(s.split()) <= 2 and not needs_retrieval(s) and not needed_action_tools(s):
        return True
    return False


def needs_retrieval(text) -> bool:
    """Есть ли в реплике ЯВНЫЕ маркеры потребности во внешних данных / URL."""
    s = normalize(text)
    if not s:
        return False
    if _has_url(s):
        return True
    return any(marker in s for marker in RETRIEVAL_MARKERS)


def is_substantive(text) -> bool:
    """Запрос достаточно содержательный, чтобы оставить модели свободу поиска.

    Переиспользуем существующий классификатор проекта search_tools.should_search
    (он же пропускает всё, что длиннее 5 слов), чтобы не плодить вторую логику.
    """
    s = normalize(text)
    if not s:
        return False
    if len(s.split()) >= _SUBSTANTIVE_MIN_WORDS:
        return True
    try:
        return bool(search_tools.should_search(text))
    except Exception:
        return False


def needed_action_tools(text) -> tuple:
    """Какие «не-поисковые» инструменты реально запрошены формулировкой."""
    s = normalize(text)
    if not s:
        return ()
    return tuple(
        name for name, marks in ACTION_MARKERS.items()
        if any(m in s for m in marks)
    )


def is_followup(text, history) -> bool:
    """Короткое продолжение прошлого хода с инструментами («а подробнее?»).

    Здесь инструменты нужны даже без маркеров: обрыв возможности продолжить
    поиск сломает уже начатую работу.
    """
    s = normalize(text)
    if not s or len(s) > _SHORT_FOLLOWUP_MAX_CHARS:
        return False
    for m in reversed(list(history or [])):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            break
        if role == "tool" or (role == "assistant" and m.get("tool_calls")):
            return True
    return False


def select_tools(user_text, all_tools, history=None,
                 allow_retrieval=True, gate_enabled=True):
    """Главная точка решения: какое подмножество tools класть в payload.

    Возвращает [] — тогда tools и tool_choice не попадут в запрос вовсе, и
    модель физически не сможет вызвать функцию (защита от поведения прокси).
    Порядок инструментов исходной схемы сохраняется.
    """
    tools = [t for t in (all_tools or []) if isinstance(t, dict)]
    if not tools or not gate_enabled:
        return tools

    names = {}
    for t in tools:
        n = (t.get("function") or {}).get("name")
        if n:
            names[n] = t

    trivial = is_trivial(user_text)
    explicit = needs_retrieval(user_text)
    event = has_event_markers(user_text)
    followup = is_followup(user_text, history or [])
    actions = needed_action_tools(user_text)
    substantive = is_substantive(user_text)
    github_link = is_github_repo_url(user_text)

    # Жёсткий отказ: тривиальная реплика без маркеров, событий и продолжения хода.
    # Отсылка к реальным событиям/новостям/датам (event) НЕ отсекается даже при
    # субъективной форме вопроса.
    if trivial and not explicit and not event and not followup and not actions and not github_link:
        return []

    wanted = set(actions)

    # Поисковые инструменты: отсылка к реальному миру, явный запрос, продолжение
    # хода, либо содержательный запрос без явных действий (модель сама решит).
    if allow_retrieval:
        if github_link and "analyze_github_repo" in names:
            # Ссылка на GitHub-репозиторий: приоритет — анализ репозитория. Базовый
            # веб-поиск (web_search/fetch_webpage) НЕ отправляем, чтобы модель не
            # «гуглила» ссылку вместо вызова специализированного инструмента.
            wanted.add("analyze_github_repo")
        elif event or followup or (explicit and not trivial):
            wanted.update(n for n in RETRIEVAL_TOOLS if n in names)
        elif not trivial and substantive and not wanted:
            wanted.update(n for n in RETRIEVAL_TOOLS if n in names)

    return [t for t in tools if (t.get("function") or {}).get("name") in wanted]


# ---------------------------------------------------------------------------
#   ЭМБЕДДИНГ-РОУТЕР (третий режим отправки тулов)
# ---------------------------------------------------------------------------
# Вместо подстроковых маркеров намерение запроса определяется по косинусному
# сходству с эталонными фразами на том же локальном эмбеддере, что и память
# профиля (FastEmbed multilingual MiniLM). Принцип: для поиска — уклон в recall
# (лучше лишний поиск, чем устаревший ответ из памяти модели), для действий —
# precision (лишний факт/файл дороже).

# Эталонные фразы намерений (по одной на каждую тул-категорию).
_EMBED_PROTOTYPES = {
    "web_search": [
        "какие сейчас модели лучшие для кодинга",
        "что нового произошло за последнее время",
        "актуальные новости на сегодня",
        "свежие события и происшествия",
        "цены и стоимость сейчас",
        "какая сейчас погода",
        "что выбрать и что лучше сейчас",
        "сравнение актуальных моделей и технологий",
        "что рекомендуешь прямо сейчас",
    ],
    "local_search": [
        "поищи в моих заметках",
        "найди в моих документах",
        "что я писал раньше в переписке",
        "поищи в истории диалогов",
        "найди в сохранённых файлах",
    ],
    "fetch_webpage": [
        "прочитай страницу по ссылке",
        "открой ссылку и перескажи",
        "что написано на этом сайте",
    ],
    "analyze_github_repo": [
        "проанализируй github репозиторий",
        "разбери этот репозиторий по ссылке",
        "что за проект на github",
    ],
    "create_downloadable_file": [
        "создай файл для скачивания",
        "сгенерируй документ docx",
        "сделай файл с текстом",
    ],
    "save_note": [
        "сохрани заметку",
        "запиши это чтобы не забыть",
        "добавь в заметки",
        "напомни мне об этом",
    ],
    "list_notes": [
        "покажи мои заметки",
        "какие заметки сохранены",
        "что в заметках",
        "напомни что в заметках",
    ],
    "remember_user_fact": [
        "запомни факт обо мне на будущее",
        "учти что я использую это",
        "сохрани в память что у меня такой стек",
        "это моё постоянное правило",
    ],
    "manage_memory": [
        "что ты обо мне знаешь",
        "покажи что ты помнишь обо мне",
        "удали факт из памяти",
        "очисти память профиля",
        "что ты помнишь обо мне",
    ],
    "set_system_prompt": [
        "измени системный промпт",
        "поменяй системный промпт",
        "обнови инструкции ассистента",
        "добавь правило в системный промпт",
    ],
}

# Пороги подобраны по реальным score'ам multilingual-MiniLM (короткие русские
# фразы дают высокую базовую схожесть, поэтому абсолютный порог на каждый тул
# заменён на сравнение «действие против поиска» + запас по отрыву от топ-действия).
_EMBED_ACTION_FLOOR = 0.60   # уверенное действие (память/файл/github)
_EMBED_SEARCH_FLOOR = 0.50   # уверенный поиск
_EMBED_MARGIN = 0.10         # допустимый отрыв «соседних» действий от топ-действия

# Действия (не поиск): при уверенном совпадении отменяют fallback «по умолчанию поиск».
_EMBED_ACTION_TOOLS = (
    "create_downloadable_file", "save_note", "list_notes",
    "remember_user_fact", "manage_memory", "set_system_prompt",
    "analyze_github_repo",
)


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _lexical_vector(text: str, dim: int = 384) -> list:
    """Детерминированный символьный вектор — fallback без FastEmbed."""
    s = normalize(text)
    vec = [0.0] * dim
    for i in range(len(s) - 1):
        vec[zlib.crc32(s[i:i + 2].encode("utf-8")) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm:
        vec = [x / norm for x in vec]
    return vec


def _embed_texts(texts) -> list:
    """Векторизует тексты локальным эмбеддером (память профиля), с fallback."""
    try:
        from memory import user_profile

        return user_profile.embed_texts(texts)
    except Exception:
        return [_lexical_vector(str(t)) for t in texts]


def select_tools_by_embedding(user_text, all_tools, history=None):
    """Эмбеддинг-роутер: какое подмножество тулов класть в payload.

    Возвращает [] для тривиальных реплик, иначе — тулы, чьё намерение совпало по
    косинусу. Содержательный запрос без уверенного действия отдаёт поисковые тулы
    (иначе модель ответит из устаревшей памяти).
    """
    tools = [t for t in (all_tools or []) if isinstance(t, dict)]
    if not tools:
        return tools
    names = {}
    for t in tools:
        n = (t.get("function") or {}).get("name")
        if n:
            names[n] = t
    if not names:
        return tools

    s = normalize(user_text)
    if not s:
        return []
    if (
        is_trivial(user_text)
        and not needs_retrieval(user_text)
        and not has_event_markers(user_text)
        and not needed_action_tools(user_text)
    ):
        return []

    phrases: list = []
    for tool in _EMBED_PROTOTYPES:
        phrases.extend(_EMBED_PROTOTYPES[tool])

    vectors = _embed_texts([s] + phrases)
    if len(vectors) < 1 + len(phrases):
        return select_tools(user_text, all_tools, history=history)

    query_vec = vectors[0]
    proto_vecs = vectors[1:]

    scores = {}
    idx = 0
    for tool in _EMBED_PROTOTYPES:
        best = 0.0
        for _p in _EMBED_PROTOTYPES[tool]:
            best = max(best, _cosine(query_vec, proto_vecs[idx]))
            idx += 1
        scores[tool] = best

    action_scores = {t: scores.get(t, 0.0) for t in _EMBED_ACTION_TOOLS}
    search_scores = {t: scores.get(t, 0.0) for t in RETRIEVAL_TOOLS}
    top_action = max(action_scores.values()) if action_scores else 0.0
    top_search = max(search_scores.values()) if search_scores else 0.0

    wanted = set()
    if top_action >= _EMBED_ACTION_FLOOR and top_action >= top_search:
        # Уверенное действие: берём топ-действие и близкие по смыслу (маржа).
        for tool, sim in action_scores.items():
            if tool in names and sim >= top_action - _EMBED_MARGIN and sim >= _EMBED_ACTION_FLOOR:
                wanted.add(tool)
    elif top_search >= _EMBED_SEARCH_FLOOR or is_substantive(user_text):
        # Уклон в поиск: уверенный поиск или содержательный запрос без действия.
        wanted.update(n for n in RETRIEVAL_TOOLS if n in names)

    return [t for t in tools if (t.get("function") or {}).get("name") in wanted]


def tool_names(tools) -> list:
    """Имена инструментов из схемы — для логов."""
    out = []
    for t in (tools or []):
        if isinstance(t, dict):
            n = (t.get("function") or {}).get("name")
            if n:
                out.append(n)
    return sorted(out)


# =====================================================================================
#   ХИРУРГИЯ СИСТЕМНОГО ПРОМПТА ПОД РЕЗУЛЬТАТ ГЕЙТА
# =====================================================================================
# Системный промпт хранится в профиле пользователя (config.json) и может быть
# произвольным — в текущем профиле это ~6.7 тыс. символов, где save_note и
# «поисковый инструмент» названы явно. Если tools в payload нет, а промпт
# инструментами всё ещё оперирует, модель начинает «вызывать» их текстом
# (галлюцинация вызова) и тратит рассуждения на решение, которого уже не
# существует. Поэтому промпт приводится в соответствие с фактическим payload:
# tools есть — подсказка про инструменты остаётся, tools нет — упоминания
# вырезаются. Правится только копия, уходящая в API: сохранённый профиль и
# история на диске не изменяются.

# Имена функций, по которым ищем упоминания в промпте.
TOOL_WORDS = (
    "save_note", "list_notes", "remember_user_fact", "manage_memory",
    "set_system_prompt", "web_search", "local_search", "fetch_webpage",
    "create_downloadable_file", "analyze_github_repo",
)

# Процедура решения — добавляется только когда инструменты реально отправлены.
TOOL_PROCEDURE = (
    "ПОРЯДОК РЕШЕНИЯ (перед КАЖДЫМ ответом):\n"
    "1) Отсылка к событиям, новостям, датам, инцидентам, организациям, моделям ИИ или версиям софта — "
    "вызови web_search (при необходимости local_search / fetch_webpage), даже если вопрос звучит как просьба о мнении. "
    "НЕ называй версии моделей по памяти.\n"
    "2) Нужны данные, которых у тебя нет — вызови local_search / web_search / fetch_webpage, "
    "затем отвечай по фактам. Ссылка на GitHub-репозиторий — analyze_github_repo.\n"
    "3) Постоянный факт о пользователе (стек, предпочтения, правила) — remember_user_fact.\n"
    "4) Записать текст в заметки — save_note; показать заметки — list_notes. Заметки ≠ память профиля.\n"
    "5) Показать/исправить/удалить/очистить память профиля — manage_memory (list/update/delete/clear).\n"
    "6) Изменить системный промпт — set_system_prompt(prompt), передай ПОЛНЫЙ новый текст.\n"
    "7) Ничего не требуется — отвечай текстом, без инструментов.\n"
    "Не вызывай инструменты «на всякий случай» и не повторяй один и тот же вызов."
)


def mentions_tools(text) -> bool:
    """Названы ли в тексте промпта доступные инструменты."""
    s = str(text or "")
    return any(w in s for w in TOOL_WORDS)


def _strip_sentences(line: str) -> str:
    """Убирает из строки только те предложения, где назван инструмент.

    Целиком строку выбрасывать нельзя: в ней могут быть и полезные правила
    («Отвечай коротко и вызови save_note» -> должно остаться «Отвечай коротко»).
    """
    if not any(w in line for w in TOOL_WORDS):
        return line
    parts = re.split(r"(?<=\.)\s+", line)
    kept = [p for p in parts if not any(w in p for w in TOOL_WORDS)]
    return " ".join(kept).strip()


def strip_tool_mentions(text) -> str:
    """Промпт без упоминаний инструментов — для ходов, где tools не отправлены."""
    s = str(text or "")
    if not mentions_tools(s):
        return s
    lines = [_strip_sentences(ln) for ln in s.split("\n")]
    out = "\n".join(lines)
    # после вырезания строк остаются «простыни» пустых строк — схлопываем
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def apply_tool_prompt(messages, tools, extra_hint=""):
    """Приводит системное сообщение копии к фактическому набору tools.

    Возвращает НОВЫЙ список (исходный не мутируется): chat_history и config.json
    остаются нетронутыми, правка живёт только внутри одного запроса.
    """
    msgs = list(messages or [])
    if not msgs or not isinstance(msgs[0], dict) or msgs[0].get("role") != "system":
        return msgs
    base = msgs[0].get("content")
    if not isinstance(base, str):
        return msgs

    if tools:
        text = base
        hint = str(extra_hint or "").strip()
        if hint and hint not in text:
            text = (text + "\n\n" + hint) if text else hint
        if TOOL_PROCEDURE not in text:
            text = (text + "\n\n" + TOOL_PROCEDURE) if text else TOOL_PROCEDURE
    else:
        text = strip_tool_mentions(base)

    out = list(msgs)
    out[0] = dict(msgs[0])
    out[0]["content"] = text
    return out
