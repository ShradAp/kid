"""Локальная память приложения.

``memory.user_profile`` — долговременный профиль пользователя: факты из диалогов
хранятся в LanceDB (``%APPDATA%/kid/data/lancedb``), эмбеддинги считаются локально
(FastEmbed/ONNX), внешние сервисы не используются. Если lancedb не установлен в
текущем интерпретаторе, хранилище автоматически переключается на локальный JSON
(``%APPDATA%/kid/data/user_facts.json``) — память продолжает работать, а ``status()``
показывает ``degraded=True`` и готовую команду установки.

Импорты ленивые по содержимому: сам пакет ничего не грузит до первого обращения
к хранилищу, поэтому старт приложения не замедляется.
"""

from memory import user_profile
from memory.user_profile import (
    DEFAULT_CATEGORY,
    FACT_CATEGORIES,
    MEMORY_BLOCK_END,
    MEMORY_BLOCK_START,
    TABLE_NAME,
    UserProfileMemory,
    build_memory_context,
    format_facts_block,
    get_store,
    install_hint,
    manage_memory,
    remember_user_fact,
    retrieve_relevant_facts,
    set_log_callback,
    stats,
    status,
    strip_memory_block,
    warmup,
)

__all__ = [
    "DEFAULT_CATEGORY",
    "FACT_CATEGORIES",
    "MEMORY_BLOCK_END",
    "MEMORY_BLOCK_START",
    "TABLE_NAME",
    "UserProfileMemory",
    "build_memory_context",
    "format_facts_block",
    "get_store",
    "install_hint",
    "manage_memory",
    "remember_user_fact",
    "retrieve_relevant_facts",
    "set_log_callback",
    "stats",
    "status",
    "strip_memory_block",
    "user_profile",
    "warmup",
]
