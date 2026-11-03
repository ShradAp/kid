"""Chat and Document Studio tool declarations without network dependencies."""

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
                "Вызывай сам, когда пользователь сообщает такой факт, без отдельной просьбы запомнить. "
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
                "Управление памятью профиля. action: list — прочитать факты при необходимости; "
                "изменение и удаление — только по явной просьбе пользователя. "
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

