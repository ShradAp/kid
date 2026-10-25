"""Сервис автоматизации документов Word (генерация и нормализация DOCX).

Публичный API:
  * :class:`DocxFormatter` — движок создания/нормализации ``.docx``;
  * :class:`StylePreset` и вспомогательные модели — описание пресета оформления;
  * :func:`resolve_preset`, :func:`load_preset`, :func:`default_gost_preset` —
    загрузка пресета из имени/пути/JSON/словаря;
  * :func:`normalize_structure`, :func:`parse_inline_runs` — разбор структуры
    контента в нормализованные блоки.
"""

from __future__ import annotations

from .docx_engine import (
    ContentBlock,
    DocxFileError,
    DocxFormatter,
    DocxStructureError,
    RunSpec,
    normalize_structure,
    parse_inline_runs,
)
from .docx_extractor import (
    AnalysisResult,
    analyze_document,
    build_preview,
    page_dict,
    preview_document,
)
from .docx_rules import RULES_CATALOG, apply_preset, apply_rules, reset_spacing
from .preset_schema import (
    Alignment,
    CaptionPlacement,
    CellVerticalAlignment,
    CleanupMode,
    CleanupPolicy,
    FigureStyle,
    HeadingStyle,
    LineSpacingRule,
    ListStyle,
    PageNumberingStyle,
    PresetError,
    SignatureLineStyle,
    SpacerMode,
    SpecialSectionsStyle,
    StylePreset,
    TableStyle,
    TextCase,
    TitlePageStyle,
    default_gost_preset,
    load_preset,
    resolve_preset,
    save_preset,
    validate_preset_payload,
)

__all__ = [
    # движок
    "DocxFormatter",
    "ContentBlock",
    "RunSpec",
    "normalize_structure",
    "parse_inline_runs",
    # анализ и правила
    "analyze_document",
    "AnalysisResult",
    "build_preview",
    "preview_document",
    "page_dict",
    "apply_rules",
    "apply_preset",
    "reset_spacing",
    "RULES_CATALOG",
    # ошибки
    "PresetError",
    "DocxStructureError",
    "DocxFileError",
    # пресеты
    "StylePreset",
    "HeadingStyle",
    "TableStyle",
    "ListStyle",
    "FigureStyle",
    "CleanupPolicy",
    "TitlePageStyle",
    "SpecialSectionsStyle",
    "PageNumberingStyle",
    "resolve_preset",
    "load_preset",
    "save_preset",
    "default_gost_preset",
    "validate_preset_payload",
    # перечисления
    "Alignment",
    "CaptionPlacement",
    "CellVerticalAlignment",
    "CleanupMode",
    "SpacerMode",
    "LineSpacingRule",
    "TextCase",
    "SignatureLineStyle",
]
