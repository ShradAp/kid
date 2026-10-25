"""Схема пресета оформления DOCX (Pydantic v2) и хранилище пресетов.

Пресет — полностью JSON-сериализуемое описание правил оформления документа:
поля страницы, основной текст, заголовки h1-h3, таблицы и подписи. Пресеты лежат
в ``presets/<preset_name>.json`` в каталоге данных пользователя; пресет по ГОСТ
(``gost_standard.json``) создаётся автоматически при первом обращении.

Модуль автономен: не требует импортов приложения, но использует ``app_paths`` для
аварийного логирования, если тот доступен.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

# --------------------------------------------------------------------------------------
# Пути: services/word_automation/ -> services/ -> корень приложения (kid/)
# --------------------------------------------------------------------------------------

_PKG_DIR = Path(__file__).resolve().parent
_APP_DIR = _PKG_DIR.parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

try:  # аварийный лог приложения — необязательная зависимость
    from app_paths import log_error as _app_log_error
except Exception:  # noqa: BLE001 - пакет должен работать и без приложения

    def _app_log_error(text: str) -> None:
        pass


PRESETS_DIRNAME = "presets"
DEFAULT_PRESET_NAME = "gost_standard"
EXTRACTOR_REPORT_NAME = "extractor_report.json"

#: Ключи уровней заголовков, которые обязаны присутствовать в пресете.
REQUIRED_HEADING_LEVELS = ("h1", "h2", "h3")
#: Все допустимые ключи уровней (h1-h3 обязательны, h4-h9 — опционально).
ALLOWED_HEADING_LEVELS = tuple(f"h{i}" for i in range(1, 10))

#: Единицы пресета: миллиметры (мм) для полей/отступов, пункты (pt) для кеглей.
#: Word хранит размеры в твипсах (1 pt = 20 твипсов, 1 мм ~ 56.69 твипса);
#: пересчёт выполняет docx_engine через docx.shared.Mm/Pt.
MM_PER_CM = 10.0

_LOGGER: Callable[[str], None] | None = None


def _log(text: str) -> None:
    try:
        if _LOGGER is not None:
            _LOGGER(text)
        else:
            _app_log_error(text)
    except Exception:  # noqa: BLE001
        pass


class PresetError(Exception):
    """Базовая ошибка работы с пресетами оформления."""


class PresetNotFoundError(PresetError):
    """Пресет с указанным именем или путём не найден."""


# --------------------------------------------------------------------------------------
# Перечисления (str-Enum: значения сериализуются в JSON как обычные строки)
# --------------------------------------------------------------------------------------


class Orientation(str, Enum):
    """Ориентация страницы."""

    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class Alignment(str, Enum):
    """Выравнивание текста параграфа или ячейки таблицы."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


class LineSpacingRule(str, Enum):
    """Правило межстрочного интервала (аналог ``WD_LINE_SPACING``).

    ``single``/``one_point_five``/``double``/``multiple`` трактуют ``line_spacing``
    как **число высот строки**; ``exactly``/``at_least`` — как **пункты**.
    """

    SINGLE = "single"
    ONE_POINT_FIVE = "one_point_five"
    DOUBLE = "double"
    MULTIPLE = "multiple"
    EXACTLY = "exactly"
    AT_LEAST = "at_least"


#: Правила, при которых ``line_spacing`` задаётся в пунктах, а не в долях строки.
POINT_BASED_RULES = frozenset({LineSpacingRule.EXACTLY, LineSpacingRule.AT_LEAST})


class TextCase(str, Enum):
    """Регистр текста заголовка."""

    NORMAL = "normal"
    UPPERCASE = "uppercase"
    TITLE = "title"


class CaptionPlacement(str, Enum):
    """Размещение подписи относительно объекта (рисунок/таблица)."""

    ABOVE = "above"
    BELOW = "below"


class CellVerticalAlignment(str, Enum):
    """Вертикальное выравнивание текста в ячейке таблицы."""

    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class CleanupMode(str, Enum):
    """Режим очистки текста при нормализации существующего документа."""

    CLEAN = "clean"
    KEEP = "keep"


class SpacerMode(str, Enum):
    """Что делать с пустыми абзацами-распорками.

    ``convert`` — удалить и компенсировать отступом ``space_before`` следующего
    блока; ``delete`` — просто удалить; ``keep`` — не трогать.
    """

    CONVERT = "convert"
    DELETE = "delete"
    KEEP = "keep"


# --------------------------------------------------------------------------------------
# Нормализация скалярных значений
# --------------------------------------------------------------------------------------

_NAMED_COLORS: dict[str, str] = {
    "auto": "auto",
    "black": "000000",
    "white": "FFFFFF",
    "red": "FF0000",
    "green": "008000",
    "blue": "0000FF",
    "navy": "000080",
    "gray": "808080",
    "grey": "808080",
    "silver": "C0C0C0",
}
_HEX6_RE = re.compile(r"^[0-9a-fA-F]{6}$")
_HEX3_RE = re.compile(r"^[0-9a-fA-F]{3}$")


def normalize_hex_color(value: Any) -> str:
    """Приводит цвет к ``RRGGBB`` либо к служебному значению ``auto``.

    Принимает ``#RRGGBB``, ``RRGGBB``, короткую запись ``RGB``, имена цвета
    (``black``, ``gray``, ...) и ``auto`` — при ``auto`` цвет не переустанавливается
    и наследуется из стиля (Word допускает ``w:color w:val="auto"``).
    """
    if value is None:
        return "auto"
    text = str(value).strip().strip("\"'").lower().lstrip("#")
    if not text:
        return "auto"
    if text in _NAMED_COLORS:
        return _NAMED_COLORS[text]
    if _HEX6_RE.match(text):
        return text.upper()
    if _HEX3_RE.match(text):
        return "".join(ch * 2 for ch in text).upper()
    raise ValueError(
        f"Некорректный цвет '{value}': ожидается RRGGBB, #RRGGBB, RGB, "
        "имя цвета или 'auto'."
    )


def normalize_heading_key(value: Any) -> str:
    """Приводит ключ уровня заголовка к виду ``h1``..``h9``.

    Допускает ``h1``, ``H1``, ``heading1``, ``Heading 1``, ``level_1``, ``1``.
    """
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s\-_]+", "", text)
    text = text.replace("heading", "").replace("level", "")
    digits = re.sub(r"\D+", "", text)
    if not digits:
        raise ValueError(f"Некорректный уровень заголовка: '{value}'")
    level = int(digits)
    if level < 1 or level > len(ALLOWED_HEADING_LEVELS):
        raise ValueError(
            f"Уровень заголовка '{value}' вне диапазона "
            f"1-{len(ALLOWED_HEADING_LEVELS)}."
        )
    return f"h{level}"


# --------------------------------------------------------------------------------------
# Базовая модель
# --------------------------------------------------------------------------------------


class _Base(BaseModel):
    """Общие настройки моделей пресета.

    ``populate_by_name`` + ``AliasChoices`` позволяют принимать как канонические
    имена полей (``font_name``), так и короткие варианты из внешних JSON
    (``name``). ``extra='ignore'`` — неизвестные ключи не ломают загрузку пресета.
    """

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        validate_assignment=True,
    )


# --------------------------------------------------------------------------------------
# 1. Страница
# --------------------------------------------------------------------------------------


class PageSettings(_Base):
    """Поля страницы и ориентация (размеры в миллиметрах)."""

    top: float = Field(20.0, ge=0.0, le=200.0, description="Верхнее поле, мм")
    bottom: float = Field(20.0, ge=0.0, le=200.0, description="Нижнее поле, мм")
    left: float = Field(
        30.0, ge=0.0, le=200.0, description="Левое поле, мм (ГОСТ — 30 мм)"
    )
    right: float = Field(
        15.0, ge=0.0, le=200.0, description="Правое поле, мм (ГОСТ — 10-15 мм)"
    )
    orientation: Orientation = Field(
        Orientation.PORTRAIT, description="Ориентация: portrait или landscape"
    )

    @field_validator("top", "bottom", "left", "right", mode="before")
    @classmethod
    def _coerce_mm(cls, value: Any) -> float:
        """Принимает число либо строку с единицей ('30mm', '3 см', '1.18in')."""
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(mm|мм|cm|см|in|\"|pt|пт)?$", text)
            if not match:
                raise ValueError(f"Не удалось разобрать размер '{value}'")
            number = float(match.group(1))
            unit = match.group(2) or "mm"
            if unit in ("cm", "см"):
                number *= MM_PER_CM
            elif unit in ("in", "\""):
                number *= 25.4
            elif unit in ("pt", "пт"):
                number *= 25.4 / 72.0
            return number
        if value is None:
            raise ValueError("Размер поля не может быть пустым")
        return float(value)

    @property
    def is_landscape(self) -> bool:
        """True для альбомной ориентации."""
        return self.orientation == Orientation.LANDSCAPE



# --------------------------------------------------------------------------------------
# 2. Основной текст
# --------------------------------------------------------------------------------------


class BodyStyle(_Base):
    """Оформление основного текста (кегли в pt, отступ первой строки в мм)."""

    font_name: str = Field(
        "Times New Roman",
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices("font_name", "name", "font"),
        description="Шрифт основного текста",
    )
    font_size: float = Field(
        14.0,
        gt=0.0,
        le=200.0,
        validation_alias=AliasChoices("font_size", "size", "pt"),
        description="Кегль, pt",
    )
    color: str = Field(
        "000000",
        validation_alias=AliasChoices("color", "font_color"),
        description="Цвет текста: RRGGBB или 'auto'",
    )
    alignment: Alignment = Field(
        Alignment.JUSTIFY, description="Выравнивание (ГОСТ — по ширине)"
    )
    line_spacing: float = Field(
        1.5, gt=0.0, le=400.0, description="Межстрочный интервал"
    )
    line_spacing_rule: LineSpacingRule = Field(
        LineSpacingRule.MULTIPLE,
        description="Правило интервала; для exactly/at_least line_spacing задаётся в pt",
    )
    first_line_indent: float = Field(
        12.5,
        ge=-200.0,
        le=200.0,
        description="Отступ первой строки, мм (ГОСТ — 12.5 мм = 1.25 см)",
    )
    space_before: float = Field(
        0.0, ge=0.0, le=400.0, description="Отступ до параграфа, pt"
    )
    space_after: float = Field(
        0.0, ge=0.0, le=400.0, description="Отступ после параграфа, pt"
    )
    keep_with_next: bool = Field(False, description="Не отрывать от следующего")
    widow_control: bool = Field(True, description="Запрет висячих строк")

    @field_validator("font_name", mode="before")
    @classmethod
    def _clean_font_name(cls, value: Any) -> str:
        text = str(value or "").strip().strip("\"'")
        return text or "Times New Roman"

    @field_validator("color", mode="before")
    @classmethod
    def _clean_color(cls, value: Any) -> str:
        return normalize_hex_color(value)

    @field_validator("font_size", "space_before", "space_after", mode="before")
    @classmethod
    def _clean_pt(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            text = re.sub(r"\s*(pt|пт|пунктов?)\s*$", "", text)
            if not text:
                raise ValueError("Пустое значение кегля/отступа")
            return float(text)
        if value is None:
            raise ValueError("Кегль/отступ не может быть пустым")
        return float(value)

    @field_validator("first_line_indent", mode="before")
    @classmethod
    def _clean_mm(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(mm|мм|cm|см)?$", text)
            if not match:
                raise ValueError(f"Не удалось разобрать отступ '{value}'")
            number = float(match.group(1))
            if match.group(2) in ("cm", "см"):
                number *= MM_PER_CM
            return number
        return float(value)

    @property
    def line_spacing_in_points(self) -> float:
        """Высота строки в пунктах — для эвристик и расчёта отступов."""
        if self.line_spacing_rule in POINT_BASED_RULES:
            return self.line_spacing
        return self.font_size * self.line_spacing


# --------------------------------------------------------------------------------------
# 3. Заголовки
# --------------------------------------------------------------------------------------


class HeadingStyle(_Base):
    """Оформление одного уровня заголовка (h1-h3, опционально h4-h9)."""

    font_name: str = Field(
        "Times New Roman",
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices("font_name", "name", "font"),
        description="Шрифт заголовка",
    )
    font_size: float = Field(
        16.0,
        gt=0.0,
        le=200.0,
        validation_alias=AliasChoices("font_size", "size", "pt"),
        description="Кегль заголовка, pt",
    )
    color: str = Field(
        "000000",
        validation_alias=AliasChoices("color", "font_color"),
        description="Цвет заголовка: RRGGBB или 'auto'",
    )
    bold: bool = Field(True, description="Полужирное начертание")
    italic: bool = Field(False, description="Курсив")
    text_case: TextCase = Field(
        TextCase.NORMAL,
        validation_alias=AliasChoices("text_case", "case", "register"),
        description="Регистр: normal, uppercase, title",
    )
    alignment: Alignment = Field(Alignment.LEFT, description="Выравнивание")
    space_before: float = Field(12.0, ge=0.0, le=400.0, description="Отступ до, pt")
    space_after: float = Field(6.0, ge=0.0, le=400.0, description="Отступ после, pt")
    page_break_before: bool = Field(
        False, description="Начинать с новой страницы (w:pageBreakBefore)"
    )
    keep_with_next: bool = Field(
        True, description="Держать заголовок вместе со следующим абзацем"
    )
    first_line_indent: float = Field(
        0.0, ge=-200.0, le=200.0, description="Отступ первой строки, мм"
    )

    @field_validator("font_name", mode="before")
    @classmethod
    def _clean_font_name(cls, value: Any) -> str:
        text = str(value or "").strip().strip("\"'")
        return text or "Times New Roman"

    @field_validator("color", mode="before")
    @classmethod
    def _clean_color(cls, value: Any) -> str:
        return normalize_hex_color(value)

    @field_validator("font_size", "space_before", "space_after", mode="before")
    @classmethod
    def _clean_pt(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            text = re.sub(r"\s*(pt|пт)\s*$", "", text)
            if not text:
                raise ValueError("Пустое значение кегля/отступа")
            return float(text)
        if value is None:
            raise ValueError("Кегль/отступ не может быть пустым")
        return float(value)

    @field_validator("first_line_indent", mode="before")
    @classmethod
    def _clean_mm(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(mm|мм|cm|см)?$", text)
            if not match:
                raise ValueError(f"Не удалось разобрать отступ '{value}'")
            number = float(match.group(1))
            if match.group(2) in ("cm", "см"):
                number *= MM_PER_CM
            return number
        return float(value)

    def apply_case(self, text: str) -> str:
        """Применяет правило регистра к тексту заголовка."""
        if self.text_case == TextCase.UPPERCASE:
            return text.upper()
        if self.text_case == TextCase.TITLE:
            return _title_case(text)
        return text


def _title_case(text: str) -> str:
    """Регистр «Как В Заголовке» с сохранением русских служебных слов строчными."""
    lowercase_words = {
        "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
        "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
        "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
        "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
        "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
        "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
        "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
        "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
        "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
        "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
        "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
        "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
        "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
        "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою", "этой",
        "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой", "им",
        "более", "всегда", "конечно", "всю", "между", "это", "of", "and", "the",
        "a", "an", "in", "on", "at", "to", "for", "with", "by", "from", "or",
    }
    words = text.split(" ")
    result: list[str] = []
    for index, word in enumerate(words):
        if not word:
            result.append(word)
            continue
        # Служебные слова остаются строчными, кроме первого слова заголовка.
        if index > 0 and word.strip("().,:;«»\"'").lower() in lowercase_words:
            result.append(word.lower() if word.isupper() else word)
            continue
        result.append(word[:1].upper() + word[1:].lower())
    return " ".join(result)


# --------------------------------------------------------------------------------------
# 4. Таблицы
# --------------------------------------------------------------------------------------


class TableStyle(_Base):
    """Оформление таблиц: текст ячеек, границы, поведение строк."""

    font_name: str = Field(
        "Times New Roman",
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices("font_name", "name", "font"),
        description="Шрифт ячеек",
    )
    font_size: float = Field(
        12.0,
        gt=0.0,
        le=200.0,
        validation_alias=AliasChoices("font_size", "size", "pt"),
        description="Кегль ячеек, pt",
    )
    color: str = Field(
        "000000",
        validation_alias=AliasChoices("color", "font_color"),
        description="Цвет текста ячеек",
    )
    alignment: Alignment = Field(Alignment.LEFT, description="Выравнивание текста")
    header_bold: bool = Field(True, description="Полужирная шапка таблицы")
    header_alignment: Alignment | None = Field(
        None, description="Выравнивание шапки (None — как у ячеек)"
    )
    borders: bool = Field(True, description="Рисовать границы таблицы")
    border_size: float = Field(
        0.5, gt=0.0, le=12.0, description="Толщина линии границы, pt"
    )
    border_color: str = Field("000000", description="Цвет границ: RRGGBB или 'auto'")
    repeat_header: bool = Field(
        True, description="Повторять шапку на новой странице (w:tblHeader)"
    )
    cant_split: bool = Field(
        True, description="Запрет разрыва строки между страницами (w:cantSplit)"
    )
    space_before: float = Field(
        6.0, ge=0.0, le=400.0, description="Отступ перед таблицей, pt"
    )
    space_after: float = Field(
        6.0, ge=0.0, le=400.0, description="Отступ после таблицы, pt"
    )
    italic: bool = Field(False, description="Курсив в ячейках таблицы")
    autofit: bool = Field(
        True, description="Автоподбор ширины столбцов (w:tblLayout autofit)"
    )
    vertical_alignment: CellVerticalAlignment = Field(
        CellVerticalAlignment.CENTER, description="Вертикальное выравнивание в ячейке"
    )
    line_spacing: float = Field(
        1.0, gt=0.0, le=400.0, description="Межстрочный интервал в ячейках"
    )
    line_spacing_rule: LineSpacingRule = Field(
        LineSpacingRule.SINGLE, description="Правило интервала в ячейках"
    )
    first_line_indent: float = Field(
        0.0, ge=-200.0, le=200.0, description="Отступ первой строки в ячейках, мм"
    )
    #: Выравнивание САМОЙ таблицы на странице (w:tblPr/w:jc). None — как в файле.
    table_alignment: Alignment | None = Field(
        None, description="Выравнивание таблицы на странице (None — как в файле)"
    )
    #: Отступы внутри ячеек (w:tblCellMar), мм.
    cell_margin_top: float = Field(
        0.0, ge=0.0, le=100.0, description="Верхний отступ в ячейке, мм"
    )
    cell_margin_bottom: float = Field(
        0.0, ge=0.0, le=100.0, description="Нижний отступ в ячейке, мм"
    )
    cell_margin_left: float = Field(
        1.9, ge=0.0, le=100.0, description="Левый отступ в ячейке, мм"
    )
    cell_margin_right: float = Field(
        1.9, ge=0.0, le=100.0, description="Правый отступ в ячейке, мм"
    )

    @field_validator("font_name", mode="before")
    @classmethod
    def _clean_font_name(cls, value: Any) -> str:
        text = str(value or "").strip().strip("\"'")
        return text or "Times New Roman"

    @field_validator("color", "border_color", mode="before")
    @classmethod
    def _clean_color(cls, value: Any) -> str:
        return normalize_hex_color(value)

    @field_validator("font_size", "space_before", "space_after", mode="before")
    @classmethod
    def _clean_pt(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            text = re.sub(r"\s*(pt|пт)\s*$", "", text)
            if not text:
                raise ValueError("Пустое значение кегля/отступа")
            return float(text)
        if value is None:
            raise ValueError("Кегль/отступ не может быть пустым")
        return float(value)

    @property
    def border_sz_eighths(self) -> int:
        """Толщина границы в восьмых долях пункта (атрибут ``w:sz``)."""
        return max(2, min(96, int(round(self.border_size * 8))))



# --------------------------------------------------------------------------------------
# 5. Подписи (рисунки и таблицы)
# --------------------------------------------------------------------------------------

_FIGURE_KINDS = frozenset({"figure", "fig", "рисунок", "рис", "image", "img", "photo"})


class CaptionStyle(_Base):
    """Правила подписей к рисункам и таблицам."""

    figure_placement: CaptionPlacement = Field(
        CaptionPlacement.BELOW, description="Подпись рисунка: сверху или снизу"
    )
    table_placement: CaptionPlacement = Field(
        CaptionPlacement.ABOVE, description="Подпись таблицы: сверху или снизу"
    )
    alignment: Alignment = Field(Alignment.CENTER, description="Выравнивание подписи")
    figure_text_format: str = Field(
        "Рисунок {n} — {text}",
        validation_alias=AliasChoices("figure_text_format", "figure_format"),
        description="Формат подписи рисунка ({n} — номер, {text} — текст)",
    )
    table_text_format: str = Field(
        "Таблица {n} — {text}",
        validation_alias=AliasChoices("table_text_format", "table_format"),
        description="Формат подписи таблицы ({n} — номер, {text} — текст)",
    )
    font_name: str = Field(
        "Times New Roman",
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices("font_name", "name", "font"),
        description="Шрифт подписи",
    )
    font_size: float = Field(
        12.0,
        gt=0.0,
        le=200.0,
        validation_alias=AliasChoices("font_size", "size", "pt"),
        description="Кегль подписи, pt",
    )
    color: str = Field(
        "000000",
        validation_alias=AliasChoices("color", "font_color"),
        description="Цвет подписи",
    )
    bold: bool = Field(False, description="Полужирная подпись")
    italic: bool = Field(False, description="Курсивная подпись")
    space_before: float = Field(3.0, ge=0.0, le=400.0, description="Отступ до, pt")
    space_after: float = Field(3.0, ge=0.0, le=400.0, description="Отступ после, pt")

    @field_validator("font_name", mode="before")
    @classmethod
    def _clean_font_name(cls, value: Any) -> str:
        text = str(value or "").strip().strip("\"'")
        return text or "Times New Roman"

    @field_validator("color", mode="before")
    @classmethod
    def _clean_color(cls, value: Any) -> str:
        return normalize_hex_color(value)

    @field_validator("font_size", "space_before", "space_after", mode="before")
    @classmethod
    def _clean_pt(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            text = re.sub(r"\s*(pt|пт)\s*$", "", text)
            if not text:
                raise ValueError("Пустое значение кегля/отступа")
            return float(text)
        if value is None:
            raise ValueError("Кегль/отступ не может быть пустым")
        return float(value)

    def placement_for(self, kind: str) -> CaptionPlacement:
        """Возвращает размещение подписи для типа объекта."""
        if str(kind or "").strip().lower() in _FIGURE_KINDS:
            return self.figure_placement
        return self.table_placement

    def format_text(self, kind: str, number: Any, text: str) -> str:
        """Собирает строку подписи по формату пресета.

        ``kind`` — ``figure`` или ``table``; ``number`` может быть ``None`` — тогда
        номер пропускается, а «повисший» разделитель убирается.
        """
        template = (
            self.figure_text_format
            if str(kind or "").strip().lower() in _FIGURE_KINDS
            else self.table_text_format
        )
        body = re.sub(r"\s+", " ", str(text or "")).strip()
        number_text = "" if number is None else str(number).strip()
        try:
            formatted = template.format(n=number_text, text=body)
        except (KeyError, IndexError, ValueError):
            # Некорректный шаблон пресета не должен ронять генерацию документа.
            _log(f"word_automation: некорректный шаблон подписи '{template}'")
            formatted = f"{number_text} {body}"
        formatted = re.sub(r"\s+", " ", formatted).strip()
        if not number_text:
            # «Рисунок  — Текст» -> «Рисунок Текст»
            formatted = re.sub(r"\s*[—–-]\s*", " ", formatted, count=1)
        return formatted.strip()
# --------------------------------------------------------------------------------------
# 6. Списки
# --------------------------------------------------------------------------------------


class ListStyle(_Base):
    """Оформление маркированных и нумерованных списков (отступы в мм)."""

    bullet_marker: str = Field(
        "–",
        min_length=1,
        max_length=10,
        validation_alias=AliasChoices("bullet_marker", "marker", "bullet"),
        description="Маркер ненумерованного списка",
    )
    numbered_format: str = Field(
        "{n}.",
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("numbered_format", "number_format"),
        description="Формат нумерации ({n} — порядковый номер)",
    )
    alignment: Alignment = Field(
        Alignment.JUSTIFY, description="Выравнивание элементов списка"
    )
    left_indent: float = Field(
        12.5, ge=0.0, le=200.0, description="Отступ списка слева, мм"
    )
    level_indent: float = Field(
        10.0, ge=0.0, le=100.0, description="Доп. отступ на уровень вложенности, мм"
    )
    space_before: float = Field(
        0.0, ge=0.0, le=400.0, description="Отступ до элемента, pt"
    )
    space_after: float = Field(
        0.0, ge=0.0, le=400.0, description="Отступ после элемента, pt"
    )
    keep_with_next: bool = Field(
        False, description="Не отрывать элемент от следующего"
    )

    @field_validator("bullet_marker", "numbered_format", mode="before")
    @classmethod
    def _clean_marker(cls, value: Any) -> str:
        text = str(value if value is not None else "").strip()
        return text or "–"

    @field_validator("left_indent", "level_indent", mode="before")
    @classmethod
    def _clean_mm(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(mm|мм|cm|см)?$", text)
            if not match:
                raise ValueError(f"Не удалось разобрать отступ '{value}'")
            number = float(match.group(1))
            if match.group(2) in ("cm", "см"):
                number *= MM_PER_CM
            return number
        return float(value)

    def marker_for(self, *, ordered: bool, number: int) -> str:
        """Возвращает маркер элемента списка с завершающим пробелом."""
        if not ordered:
            return f"{self.bullet_marker} "
        try:
            return f"{self.numbered_format.format(n=number)} "
        except (KeyError, IndexError, ValueError):
            _log(f"word_automation: некорректный формат нумерации '{self.numbered_format}'")
            return f"{number}. "


# --------------------------------------------------------------------------------------
# 6b. Графические элементы (рисунки)
# --------------------------------------------------------------------------------------


class FigureStyle(_Base):
    """Правила размещения графических элементов (рисунков) в документе."""

    alignment: Alignment = Field(
        Alignment.CENTER, description="Выравнивание рисунков на странице"
    )


# --------------------------------------------------------------------------------------
# 7. Правила вычистки «грязного» форматирования
# --------------------------------------------------------------------------------------


class CleanupPolicy(_Base):
    """Правила нормализации существующего документа (``normalize_document``)."""

    text_mode: CleanupMode = Field(
        CleanupMode.CLEAN, description="Чистить текст (пробелы/табы/NBSP) или нет"
    )
    spacer_mode: SpacerMode = Field(
        SpacerMode.CONVERT, description="Что делать с пустыми абзацами-распорками"
    )
    max_consecutive_spaces: int = Field(
        1, ge=1, le=10, description="Сколько пробелов подряд оставлять после сжатия"
    )
    normalize_nbsp: bool = Field(
        True, description="Заменять неразрывные пробелы обычными"
    )
    strip_edges: bool = Field(
        True, description="Убирать пробелы в начале и в конце абзаца"
    )
    fix_space_alignment: bool = Field(
        True, description="Заменять «выравнивание пробелами» настоящим выравниванием"
    )
    alignment_shift_spaces: int = Field(
        3, ge=1, le=40, description="Порог ведущих пробелов, считающийся сдвигом"
    )
    spacer_spacing: float = Field(
        6.0, ge=0.0, le=200.0, description="Отступ за один удалённый пустой абзац, pt"
    )
    max_spacers: int = Field(
        4, ge=0, le=40, description="Сколько распорок подряд компенсировать отступом"
    )
    preserve_title_page_spacers: bool = Field(
        True,
        description=(
            "Не удалять пустые абзацы-распорки на титульном листе: они служат "
            "вертикальными распорками"
        ),
    )
    preserve_special_spacers: bool = Field(
        True,
        description=(
            "Не удалять пустые абзацы-распорки на бланке задания и в спецразделах "
            "(пока не реализована жёсткая табличная сетка)"
        ),
    )

    @field_validator("spacer_spacing", mode="before")
    @classmethod
    def _clean_pt(cls, value: Any) -> float:
        if isinstance(value, str):
            text = value.strip().lower().replace(",", ".")
            text = re.sub(r"\s*(pt|пт)\s*$", "", text)
            if not text:
                raise ValueError("Пустое значение отступа")
            return float(text)
        if value is None:
            raise ValueError("Отступ не может быть пустым")
        return float(value)


# --------------------------------------------------------------------------------------
# 8. Титульный лист, специальные разделы и нумерация страниц
# --------------------------------------------------------------------------------------


class SignatureLineStyle(str, Enum):
    """Способ оформления линии для подписи на титульном листе.

    ``underline`` — подчёркивание рана; ``tab`` — табуляция с заполнителем
    (``w:tab`` + ``w:leader``); ``table`` — невидимая таблица-сетка с нижней
    границей; ``text`` — линия символами ``____``; ``none`` — без линии.
    """

    UNDERLINE = "underline"
    TAB = "tab"
    TABLE = "table"
    TEXT = "text"
    NONE = "none"


#: Секции, к которым может применяться блок ``special_sections``.
SPECIAL_SECTION_KINDS = ("referat", "task", "content")


def _clean_font_name(value: Any) -> str:
    """Нормализует имя гарнитуры (общий валидатор для блоков схемы)."""
    text = str(value or "").strip().strip("\"'")
    return text or "Times New Roman"


def _clean_color_value(value: Any) -> str:
    """Нормализует цвет (общий валидатор для блоков схемы)."""
    return normalize_hex_color(value)


def _clean_pt_value(value: Any) -> float:
    """Принимает кегль/отступ в пунктах (число либо строка '14pt')."""
    if isinstance(value, str):
        text = value.strip().lower().replace(",", ".")
        text = re.sub(r"\s*(pt|пт|пунктов?)\s*$", "", text)
        if not text:
            raise ValueError("Пустое значение кегля/отступа")
        return float(text)
    if value is None:
        raise ValueError("Кегль/отступ не может быть пустым")
    return float(value)


def _clean_mm_value(value: Any) -> float:
    """Принимает отступ в миллиметрах (число либо строка '85мм'/'8.5 см')."""
    if isinstance(value, str):
        text = value.strip().lower().replace(",", ".")
        match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(mm|мм|cm|см)?$", text)
        if not match:
            raise ValueError(f"Не удалось разобрать отступ '{value}'")
        number = float(match.group(1))
        if match.group(2) in ("cm", "см"):
            number *= MM_PER_CM
        return number
    return float(value)


#: Канонические заполнители табуляции (``w:leader``): dot/hyphen/underscore/none.
TAB_LEADERS = ("dot", "hyphen", "underscore", "none")


def normalize_tab_leader(value: Any) -> str:
    """Приводит заполнитель табуляции к каноническому значению."""
    text = str(value or "none").strip().lower()
    mapping = {
        "dot": "dot", "dots": "dot", "точки": "dot", "…": "dot",
        "hyphen": "hyphen", "dash": "hyphen", "dashes": "hyphen", "дефис": "hyphen",
        "underscore": "underscore", "line": "underscore", "lines": "underscore",
        "_": "underscore", "подчерк": "underscore",
        "none": "none", "spaces": "none", "": "none",
    }
    return mapping.get(text, "none")


class _BlockTextStyle(_Base):
    """Общая база для текстовых блоков титульного листа (шрифт + интервалы)."""

    font_name: str = Field(
        "Times New Roman",
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices("font_name", "name", "font"),
        description="Гарнитура",
    )
    font_size: float = Field(
        14.0,
        gt=0.0,
        le=200.0,
        validation_alias=AliasChoices("font_size", "size", "pt"),
        description="Кегль, pt",
    )
    color: str = Field(
        "000000",
        validation_alias=AliasChoices("color", "font_color"),
        description="Цвет текста: RRGGBB или 'auto'",
    )
    bold: bool = Field(False, description="Полужирный")
    italic: bool = Field(False, description="Курсив")
    alignment: Alignment = Field(Alignment.CENTER, description="Выравнивание")
    space_before: float = Field(
        0.0, ge=0.0, le=400.0, description="Отступ до блока, pt"
    )
    space_after: float = Field(
        0.0, ge=0.0, le=400.0, description="Отступ после блока, pt"
    )

    @field_validator("font_name", mode="before")
    @classmethod
    def _v_font(cls, value: Any) -> str:
        return _clean_font_name(value)

    @field_validator("color", mode="before")
    @classmethod
    def _v_color(cls, value: Any) -> str:
        return _clean_color_value(value)

    @field_validator("font_size", "space_before", "space_after", mode="before")
    @classmethod
    def _v_pt(cls, value: Any) -> float:
        return _clean_pt_value(value)


class TitlePageHeaderStyle(_BlockTextStyle):
    """Шапка титульного листа (министерство/учебное заведение/кафедра)."""

    space_after: float = Field(
        14.0, ge=0.0, le=400.0, description="Отступ после шапки, pt"
    )


class TitlePageTitleStyle(_BlockTextStyle):
    """Заголовок работы на титульном листе («ОТЧЕТ», «КУРСОВАЯ РАБОТА»)."""

    bold: bool = Field(True, description="Заголовок работы обычно полужирный")
    space_before: float = Field(
        40.0, ge=0.0, le=400.0, description="Отступ до заголовка работы, pt"
    )
    space_after: float = Field(
        40.0, ge=0.0, le=400.0, description="Отступ после заголовка работы, pt"
    )


class TitlePageSignaturesStyle(_BlockTextStyle):
    """Блок подписей («Выполнил: …», «Проверил: …»)."""

    alignment: Alignment = Field(
        Alignment.RIGHT, description="Подписи смещены вправо"
    )
    left_indent: float = Field(
        85.0,
        ge=0.0,
        le=200.0,
        description="Отступ слева для блока подписей, мм (по умолчанию ~85 мм)",
    )
    line_style: SignatureLineStyle = Field(
        SignatureLineStyle.UNDERLINE,
        description="Оформление линии подписи: underline/tab/table/text/none",
    )
    line_width: float = Field(
        40.0, ge=5.0, le=200.0, description="Длина линии подписи, мм (для tab/table)"
    )
    preserve_tabs: bool = Field(
        True,
        description=(
            "Сохранять табуляцию в строке подписи: текст до таба слева, черта "
            "подписи — по правой границе поля"
        ),
    )
    tab_stop_mm: float = Field(
        165.0,
        ge=0.0,
        le=250.0,
        description="Позиция правой табуляции для подписей, мм (правое поле A4)",
    )
    tab_leader: str = Field(
        "underscore",
        description="Заполнитель табуляции подписи: dot/hyphen/underscore/none",
    )
    space_before: float = Field(
        24.0, ge=0.0, le=400.0, description="Отступ до блока подписей, pt"
    )

    @field_validator("left_indent", "tab_stop_mm", mode="before")
    @classmethod
    def _v_indent(cls, value: Any) -> float:
        return _clean_mm_value(value)

    @field_validator("tab_leader", mode="before")
    @classmethod
    def _v_leader(cls, value: Any) -> str:
        return normalize_tab_leader(value)


class TitlePageFooterStyle(_BlockTextStyle):
    """Подвал титульного листа (город и год)."""

    alignment: Alignment = Field(Alignment.CENTER, description="Город и год по центру")
    anchor_bottom: bool = Field(
        True, description="Прижать блок к нижнему полю страницы"
    )
    space_before: float = Field(
        240.0, ge=0.0, le=1000.0, description="Отступ до подвала, pt (распорка вниз)"
    )


class TitlePageStyle(_Base):
    """Оформление титульного листа: шапка, заголовок работы, подписи, подвал."""

    enabled: bool = Field(
        True, description="Оформлять титульный лист отдельными правилами"
    )
    own_section: bool = Field(
        True, description="Выделять титульный лист в отдельную секцию Word"
    )
    distribute_vertically: bool = Field(
        True,
        description="Распределять блоки по вертикали (шапка вверх, подвал к низу)",
    )
    header: TitlePageHeaderStyle = Field(default_factory=TitlePageHeaderStyle)
    title: TitlePageTitleStyle = Field(default_factory=TitlePageTitleStyle)
    signatures: TitlePageSignaturesStyle = Field(
        default_factory=TitlePageSignaturesStyle
    )
    footer: TitlePageFooterStyle = Field(default_factory=TitlePageFooterStyle)
    title_keywords: list[str] = Field(
        default_factory=lambda: [
            "отчёт", "отчет", "курсовая", "диплом", "работа", "проект",
            "реферат", "лабораторная", "практическая", "report",
        ],
        description="Слова, по которым распознаётся заголовок работы",
    )
    signature_keywords: list[str] = Field(
        default_factory=lambda: [
            "выполнил", "выполнила", "студент", "группа", "проверил",
            "проверила", "преподаватель", "руководитель", "подпись", "н.",
        ],
        description="Слова, по которым распознаётся блок подписей",
    )


class SpecialSectionsStyle(_Base):
    """Оформление специальных разделов (реферат / задание / содержание).

    Название раздела («РЕФЕРАТ», «ЗАДАНИЕ», «СОДЕРЖАНИЕ») оформляется полями
    ``title_*`` (строго по центру, без красной строки), основной текст — полями
    ``body_*`` (по ширине, абзацный отступ, полуторный интервал).
    """

    enabled: bool = Field(True, description="Применять правила спецразделов")
    apply_to: list[str] = Field(
        default_factory=lambda: list(SPECIAL_SECTION_KINDS),
        description="Какие разделы оформлять: referat/task/content",
    )
    title_font_name: str = Field("Times New Roman", min_length=1, max_length=80)
    title_font_size: float = Field(14.0, gt=0.0, le=200.0)
    title_bold: bool = Field(True, description="Название раздела полужирным")
    title_italic: bool = Field(False)
    title_color: str = Field("000000")
    title_alignment: Alignment = Field(
        Alignment.CENTER, description="Название раздела строго по центру"
    )
    title_text_case: TextCase = Field(
        TextCase.UPPERCASE, description="Регистр названия раздела"
    )
    title_first_line_indent: float = Field(
        0.0, ge=-200.0, le=200.0, description="Название раздела без красной строки, мм"
    )
    title_space_before: float = Field(0.0, ge=0.0, le=400.0)
    title_space_after: float = Field(12.0, ge=0.0, le=400.0)
    body_font_name: str = Field("Times New Roman", min_length=1, max_length=80)
    body_font_size: float = Field(14.0, gt=0.0, le=200.0)
    body_alignment: Alignment = Field(
        Alignment.JUSTIFY, description="Основной текст раздела по ширине"
    )
    body_first_line_indent: float = Field(
        12.5, ge=-200.0, le=200.0, description="Абзацный отступ текста раздела, мм"
    )
    body_line_spacing: float = Field(1.5, gt=0.0, le=400.0)
    body_line_spacing_rule: LineSpacingRule = Field(LineSpacingRule.MULTIPLE)
    referat_titles: list[str] = Field(default_factory=lambda: ["реферат"])
    task_titles: list[str] = Field(default_factory=lambda: ["задание"])
    content_titles: list[str] = Field(
        default_factory=lambda: ["содержание", "оглавление"]
    )
    # --- разрывы страниц перед структурными разделами ---
    page_break_before: bool = Field(
        True,
        description=(
            "Каждый структурный элемент начинается со строго новой страницы "
            "(РЕФЕРАТ, СОДЕРЖАНИЕ, ВВЕДЕНИЕ, ЗАКЛЮЧЕНИЕ, СПИСКИ, ПРИЛОЖЕНИЯ)"
        ),
    )
    structural_titles: list[str] = Field(
        default_factory=lambda: [
            "реферат", "содержание", "оглавление", "введение", "заключение",
            "список", "перечень", "приложение", "обозначен", "сокращен",
            "определения", "термины", "аннотация", "выводы",
        ],
        description="Ключевые слова структурных разделов (начинаются с новой страницы)",
    )
    # --- оформление содержания (TOC) ---
    toc_right_tab_mm: float = Field(
        165.0,
        ge=0.0,
        le=250.0,
        description=(
            "Позиция правой табуляции для номеров страниц содержания, мм "
            "(165 мм — правый край текста A4 при полях 30/15 мм)"
        ),
    )
    toc_leader: str = Field(
        "dot", description="Заполнитель табуляции содержания: dot/hyphen/underscore/none"
    )
    toc_rebuild_entries: bool = Field(
        True,
        description=(
            "Пересобирать строки содержания в вид «Заголовок<таб>стр.» с "
            "точечным заполнителем, прижатым к правому полю"
        ),
    )
    toc_entry_alignment: Alignment = Field(
        Alignment.LEFT, description="Выравнивание строки содержания (не считая табуляции)"
    )

    @field_validator("title_font_name", "body_font_name", mode="before")
    @classmethod
    def _v_font(cls, value: Any) -> str:
        return _clean_font_name(value)

    @field_validator("title_color", mode="before")
    @classmethod
    def _v_color(cls, value: Any) -> str:
        return _clean_color_value(value)

    @field_validator(
        "title_font_size", "title_space_before", "title_space_after",
        "body_font_size", mode="before",
    )
    @classmethod
    def _v_pt(cls, value: Any) -> float:
        return _clean_pt_value(value)

    @field_validator("toc_right_tab_mm", mode="before")
    @classmethod
    def _v_toc_mm(cls, value: Any) -> float:
        return _clean_mm_value(value)

    @field_validator("toc_leader", mode="before")
    @classmethod
    def _v_toc_leader(cls, value: Any) -> str:
        return normalize_tab_leader(value)

    @field_validator(
        "title_first_line_indent", "body_first_line_indent", mode="before"
    )
    @classmethod
    def _v_indent(cls, value: Any) -> float:
        return _clean_mm_value(value)

    def titles_for(self, kind: str) -> list[str]:
        """Ключевые заголовки раздела (в нижнем регистре) для типа ``kind``."""
        mapping = {
            "referat": self.referat_titles,
            "task": self.task_titles,
            "content": self.content_titles,
        }
        return [str(t).strip().lower() for t in mapping.get(kind, []) if str(t).strip()]


class PageNumberingStyle(_Base):
    """Нумерация страниц в колонтитуле (по умолчанию — нижний, по центру)."""

    enabled: bool = Field(False, description="Включать нумерацию страниц")
    position: str = Field(
        "footer", pattern="^(footer|header)$", description="Колонтитул: footer/header"
    )
    alignment: Alignment = Field(Alignment.CENTER, description="Положение номера")
    start_at: int = Field(
        1, ge=1, le=10000, description="Значение номера на первой странице (w:pgNumType)"
    )
    show_from_page: int = Field(
        3, ge=1, le=10000, description="Физическая страница, с которой виден номер"
    )
    show_on_first_page: bool = Field(
        False, description="Показывать номер на титульном листе (страница 1)"
    )
    show_on_task_page: bool = Field(
        False, description="Показывать номер на странице задания (обычно страница 2)"
    )
    hide_on_special: bool = Field(
        True, description="Скрывать номер на страницах титульника и задания"
    )
    page_format: str = Field(
        "{PAGE}", description="Формат номера: {PAGE} подставляется как поле PAGE"
    )
    font_name: str = Field("Times New Roman", min_length=1, max_length=80)
    font_size: float = Field(12.0, gt=0.0, le=200.0)

    @field_validator("font_name", mode="before")
    @classmethod
    def _v_font(cls, value: Any) -> str:
        return _clean_font_name(value)

    @field_validator("font_size", mode="before")
    @classmethod
    def _v_pt(cls, value: Any) -> float:
        return _clean_pt_value(value)

    @field_validator("position", mode="before")
    @classmethod
    def _v_position(cls, value: Any) -> str:
        text = str(value or "footer").strip().lower()
        return "header" if text.startswith("head") else "footer"


# --------------------------------------------------------------------------------------
# Корневая модель пресета
# --------------------------------------------------------------------------------------


class StylePreset(_Base):
    """Полный пресет оформления DOCX — сериализуется в JSON без потерь."""

    name: str = Field(
        DEFAULT_PRESET_NAME,
        min_length=1,
        max_length=80,
        description="Имя пресета (используется как имя файла в presets/)",
    )
    description: str = Field("", max_length=500, description="Пояснение к пресету")
    page: PageSettings = Field(default_factory=PageSettings)
    body: BodyStyle = Field(default_factory=BodyStyle)
    headings: dict[str, HeadingStyle] = Field(default_factory=dict)
    tables: TableStyle = Field(default_factory=TableStyle)
    captions: CaptionStyle = Field(default_factory=CaptionStyle)
    figures: FigureStyle = Field(default_factory=FigureStyle)
    lists: ListStyle = Field(default_factory=ListStyle)
    cleanup: CleanupPolicy = Field(default_factory=CleanupPolicy)
    title_page: TitlePageStyle = Field(default_factory=TitlePageStyle)
    special_sections: SpecialSectionsStyle = Field(
        default_factory=SpecialSectionsStyle
    )
    page_numbering: PageNumberingStyle = Field(default_factory=PageNumberingStyle)

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or DEFAULT_PRESET_NAME

    @field_validator("headings", mode="before")
    @classmethod
    def _coerce_headings(cls, value: Any) -> dict[str, Any]:
        """Принимает словарь или список настроек и нормализует ключи ``h1``..``h9``."""
        if value is None:
            return {}
        if isinstance(value, (list, tuple)):
            value = {f"h{i + 1}": item for i, item in enumerate(value)}
        if not isinstance(value, dict):
            raise ValueError(
                "headings должен быть словарём {h1: {...}, h2: {...}, h3: {...}} "
                f"или списком, получено: {type(value).__name__}"
            )
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized[normalize_heading_key(key)] = item
        return normalized

    def model_post_init(self, __context: Any) -> None:
        """Гарантирует наличие обязательных уровней h1-h3 (каскад от h1 к h3)."""
        self._fill_missing_headings()

    def _fill_missing_headings(self) -> None:
        defaults = default_headings()
        for level in REQUIRED_HEADING_LEVELS:
            if level in self.headings:
                continue
            # Каскад: недостающий уровень наследуется от предыдущего либо дефолта.
            previous = self.headings.get(f"h{int(level[1]) - 1}")
            if previous is not None:
                self.headings[level] = previous.model_copy(
                    update={"font_size": defaults[level].font_size}
                )
            else:
                self.headings[level] = defaults[level]

    def heading(self, level: int | str) -> HeadingStyle:
        """Возвращает настройки уровня; неизвестный уровень округляется к ближайшему."""
        key = normalize_heading_key(level)
        if key in self.headings:
            return self.headings[key]
        fallback = max(
            (int(k[1:]) for k in self.headings), default=1
        )
        nearest = f"h{min(max(int(key[1:]), 1), max(fallback, 1))}"
        return self.headings.get(nearest) or default_headings()[nearest]

    def to_dict(self) -> dict[str, Any]:
        """JSON-совместимый словарь (перечисления — строки)."""
        return self.model_dump(mode="json", by_alias=False)

    def to_json(self, *, indent: int = 2) -> str:
        """Текст JSON для записи в ``presets/<name>.json``."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def clone(self, **updates: Any) -> "StylePreset":
        """Копия пресета с точечными изменениями (не мутирует исходный)."""
        data = self.to_dict()
        data.update(updates)
        return StylePreset.model_validate(data)

    def summary(self) -> dict[str, Any]:
        """Краткая сводка — удобно отдавать модели/в лог."""
        return {
            "name": self.name,
            "page": {
                "margins_mm": {
                    "top": self.page.top,
                    "bottom": self.page.bottom,
                    "left": self.page.left,
                    "right": self.page.right,
                },
                "orientation": self.page.orientation.value,
            },
            "body": {
                "font": self.body.font_name,
                "size_pt": self.body.font_size,
                "alignment": self.body.alignment.value,
                "line_spacing": self.body.line_spacing,
                "first_line_indent_mm": self.body.first_line_indent,
            },
            "headings": {
                key: {
                    "size_pt": style.font_size,
                    "bold": style.bold,
                    "case": style.text_case.value,
                    "page_break_before": style.page_break_before,
                }
                for key, style in sorted(self.headings.items())
            },
            "tables": {
                "font": self.tables.font_name,
                "size_pt": self.tables.font_size,
                "borders": self.tables.borders,
                "repeat_header": self.tables.repeat_header,
                "cant_split": self.tables.cant_split,
            },
            "title_page": {
                "enabled": self.title_page.enabled,
                "own_section": self.title_page.own_section,
                "distribute_vertically": self.title_page.distribute_vertically,
                "title_alignment": self.title_page.title.alignment.value,
                "signatures_alignment": self.title_page.signatures.alignment.value,
                "signatures_left_indent_mm": self.title_page.signatures.left_indent,
                "footer_anchor_bottom": self.title_page.footer.anchor_bottom,
            },
            "special_sections": {
                "enabled": self.special_sections.enabled,
                "apply_to": list(self.special_sections.apply_to),
                "title_alignment": self.special_sections.title_alignment.value,
                "title_first_line_indent_mm": (
                    self.special_sections.title_first_line_indent
                ),
                "body_alignment": self.special_sections.body_alignment.value,
                "body_first_line_indent_mm": (
                    self.special_sections.body_first_line_indent
                ),
                "body_line_spacing": self.special_sections.body_line_spacing,
            },
            "page_numbering": {
                "enabled": self.page_numbering.enabled,
                "position": self.page_numbering.position,
                "alignment": self.page_numbering.alignment.value,
                "start_at": self.page_numbering.start_at,
                "show_from_page": self.page_numbering.show_from_page,
                "hide_on_special": self.page_numbering.hide_on_special,
            },
        }


# --------------------------------------------------------------------------------------
# Дефолтные значения (ГОСТ 7.32: Times New Roman 14 pt, интервал 1.5,
# абзацный отступ 1.25 см, поля: левое 30 мм, правое 10-15 мм, верх/низ 20 мм)
# --------------------------------------------------------------------------------------


def default_headings() -> dict[str, HeadingStyle]:
    """Заголовки h1-h3 по умолчанию (шрифт основного текста, кегль крупнее)."""
    return {
        "h1": HeadingStyle(
            font_name="Times New Roman",
            font_size=16.0,
            bold=True,
            italic=False,
            text_case=TextCase.NORMAL,
            alignment=Alignment.CENTER,
            space_before=0.0,
            space_after=12.0,
            # ГОСТ 7.32: каждую структурную часть начинают с нового листа.
            page_break_before=True,
            keep_with_next=True,
            first_line_indent=0.0,
        ),
        "h2": HeadingStyle(
            font_name="Times New Roman",
            font_size=14.0,
            bold=True,
            italic=False,
            text_case=TextCase.NORMAL,
            alignment=Alignment.LEFT,
            space_before=12.0,
            space_after=6.0,
            page_break_before=False,
            keep_with_next=True,
            first_line_indent=0.0,
        ),
        "h3": HeadingStyle(
            font_name="Times New Roman",
            font_size=14.0,
            bold=True,
            italic=True,
            text_case=TextCase.NORMAL,
            alignment=Alignment.LEFT,
            space_before=10.0,
            space_after=6.0,
            page_break_before=False,
            keep_with_next=True,
            first_line_indent=0.0,
        ),
    }


def default_gost_preset() -> StylePreset:
    """Дефолтный пресет оформления по ГОСТ."""
    return StylePreset(
        name=DEFAULT_PRESET_NAME,
        description=(
            "Оформление по ГОСТ 7.32: Times New Roman 14 pt, полуторный интервал, "
            "абзацный отступ 1.25 см, выравнивание по ширине, поля 20/20/30/15 мм."
        ),
        page=PageSettings(
            top=20.0, bottom=20.0, left=30.0, right=15.0,
            orientation=Orientation.PORTRAIT,
        ),
        body=BodyStyle(
            font_name="Times New Roman",
            font_size=14.0,
            color="000000",
            alignment=Alignment.JUSTIFY,
            line_spacing=1.5,
            line_spacing_rule=LineSpacingRule.MULTIPLE,
            first_line_indent=12.5,
            space_before=0.0,
            space_after=0.0,
            keep_with_next=False,
            widow_control=True,
        ),
        headings=default_headings(),
        tables=TableStyle(
            font_name="Times New Roman",
            font_size=12.0,
            alignment=Alignment.LEFT,
            header_bold=True,
            borders=True,
            border_size=0.5,
            repeat_header=True,
            cant_split=True,
        ),
        captions=CaptionStyle(
            figure_placement=CaptionPlacement.BELOW,
            table_placement=CaptionPlacement.ABOVE,
            alignment=Alignment.CENTER,
            font_name="Times New Roman",
            font_size=12.0,
            bold=False,
            italic=False,
        ),
        figures=FigureStyle(alignment=Alignment.CENTER),
        lists=ListStyle(
            bullet_marker="–",
            numbered_format="{n}.",
            alignment=Alignment.JUSTIFY,
            left_indent=12.5,
            level_indent=10.0,
            space_before=0.0,
            space_after=0.0,
            keep_with_next=False,
        ),
        cleanup=CleanupPolicy(
            text_mode=CleanupMode.CLEAN,
            spacer_mode=SpacerMode.CONVERT,
            max_consecutive_spaces=1,
            normalize_nbsp=True,
            strip_edges=True,
            fix_space_alignment=True,
            alignment_shift_spaces=3,
            spacer_spacing=6.0,
            max_spacers=4,
            preserve_title_page_spacers=True,
        ),
        title_page=TitlePageStyle(
            enabled=True,
            own_section=True,
            distribute_vertically=True,
            header=TitlePageHeaderStyle(
                font_name="Times New Roman", font_size=14.0,
                bold=False, alignment=Alignment.CENTER, space_after=14.0,
            ),
            title=TitlePageTitleStyle(
                font_name="Times New Roman", font_size=14.0,
                bold=True, alignment=Alignment.CENTER,
                # Вертикальные отступы уменьшены (было 40/40 + 24 + 240): с ними
                # титульный лист не помещался на одной странице A4, а «ЗАДАНИЕ»
                # уезжало на лист 3 вместо листа 2.
                space_before=24.0, space_after=24.0,
            ),
            signatures=TitlePageSignaturesStyle(
                font_name="Times New Roman", font_size=14.0,
                alignment=Alignment.RIGHT, left_indent=85.0,
                line_style=SignatureLineStyle.UNDERLINE, space_before=12.0,
            ),
            footer=TitlePageFooterStyle(
                font_name="Times New Roman", font_size=14.0,
                alignment=Alignment.CENTER, anchor_bottom=True, space_before=120.0,
            ),
        ),
        special_sections=SpecialSectionsStyle(
            enabled=True,
            apply_to=list(SPECIAL_SECTION_KINDS),
            title_font_name="Times New Roman", title_font_size=14.0,
            title_bold=True, title_alignment=Alignment.CENTER,
            title_text_case=TextCase.UPPERCASE,
            title_first_line_indent=0.0,
            title_space_before=0.0, title_space_after=12.0,
            body_font_name="Times New Roman", body_font_size=14.0,
            body_alignment=Alignment.JUSTIFY,
            body_first_line_indent=12.5,
            body_line_spacing=1.5,
            body_line_spacing_rule=LineSpacingRule.MULTIPLE,
        ),
        page_numbering=PageNumberingStyle(
            enabled=False,
            position="footer",
            alignment=Alignment.CENTER,
            start_at=1,
            show_from_page=3,
            show_on_first_page=False,
            show_on_task_page=False,
            hide_on_special=True,
            font_name="Times New Roman", font_size=12.0,
        ),
    )


# --------------------------------------------------------------------------------------
# Хранилище пресетов: presets/<preset_name>.json
# --------------------------------------------------------------------------------------


def presets_dir() -> Path:
    """Каталог пресетов (создаётся при обращении).

    По умолчанию ``<каталог данных пользователя>/presets``; переопределяется переменной
    окружения ``KID_WORD_PRESETS_DIR``.
    """
    override = os.environ.get("KID_WORD_PRESETS_DIR", "").strip().strip("\"'")
    if override:
        base = Path(os.path.expanduser(os.path.expandvars(override)))
    else:
        from app_paths import USER_DATA_DIR

        base = USER_DATA_DIR / PRESETS_DIRNAME
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"word_automation: не удалось создать каталог пресетов {base}: {exc}")
    return base


def safe_preset_name(name: str) -> str:
    """Имя пресета, безопасное для использования как имя файла."""
    text = str(name or "").strip().replace("\\", "/").split("/")[-1]
    text = re.sub(r"[\x00-\x1f<>:\"\\|?*]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip("._")
    if text.lower().endswith(".json"):
        text = text[:-5]
    text = text[:80].strip("._")
    return text or DEFAULT_PRESET_NAME


def preset_path(name: str) -> Path:
    """Полный путь к ``presets/<preset_name>.json``."""
    return presets_dir() / f"{safe_preset_name(name)}.json"


def list_presets() -> list[str]:
    """Имена доступных пресетов (без расширения), отсортированные."""
    try:
        return sorted(
            item.stem for item in presets_dir().glob("*.json") if item.is_file()
        )
    except OSError as exc:
        _log(f"word_automation: не удалось прочитать каталог пресетов: {exc}")
        return []


def save_preset(preset: StylePreset, name: str | None = None) -> str:
    """Сохраняет пресет в ``presets/<name>.json`` и возвращает путь к файлу."""
    if not isinstance(preset, StylePreset):
        raise PresetError(
            f"save_preset ожидает StylePreset, получено: {type(preset).__name__}"
        )
    target_name = safe_preset_name(name or preset.name)
    if target_name != preset.name:
        preset = preset.clone(name=target_name)
    path = preset_path(target_name)
    payload = preset.to_json(indent=2)
    # Атомарная запись: сначала временный файл, затем замена — чтобы пресет не
    # оказался «побитым» при обрыве записи.
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise PresetError(
            f"Не удалось сохранить пресет '{target_name}': {exc}"
        ) from exc
    return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    """Читает JSON пресета с внятными ошибками."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PresetNotFoundError(
            f"Не удалось прочитать файл пресета {path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PresetError(
            f"Файл пресета {path} содержит некорректный JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PresetError(
            f"Пресет {path} должен быть JSON-объектом, "
            f"получено: {type(data).__name__}"
        )
    return data


def ensure_default_preset(*, force: bool = False) -> str:
    """Создаёт дефолтный пресет по ГОСТ, если его ещё нет. Возвращает путь."""
    path = preset_path(DEFAULT_PRESET_NAME)
    if path.exists() and not force:
        return str(path)
    saved = save_preset(default_gost_preset())
    _log(f"word_automation: создан дефолтный пресет ГОСТ -> {saved}")
    return saved


def _validate_preset_dict(data: dict[str, Any], origin: str) -> StylePreset:
    """Валидирует словарь пресета с внятным сообщением об ошибке."""
    try:
        return StylePreset.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()[:6]
        )
        raise PresetError(
            f"Пресет {origin} не прошёл валидацию: {problems}"
        ) from exc


def load_preset(
    source: "StylePreset | str | Path | dict[str, Any] | None" = None,
) -> StylePreset:
    """Загружает пресет из имени, пути, словаря или возвращает переданный объект.

    Поддерживаемые варианты ``source``:
      * ``StylePreset`` — возвращается как есть;
      * ``dict`` — валидируется Pydantic-моделью;
      * ``str``/``Path`` — имя пресета в ``presets/``, путь к ``.json``
        либо inline-JSON (строка, начинающаяся с ``{``);
      * ``None``/пустая строка — дефолтный пресет по ГОСТ.

    Если дефолтный пресет отсутствует на диске — он создаётся автоматически.
    """
    if source is None:
        source = DEFAULT_PRESET_NAME
    if isinstance(source, StylePreset):
        return source
    if isinstance(source, Path):
        if not source.exists():
            raise PresetNotFoundError(f"Файл пресета не найден: {source}")
        return _validate_preset_dict(_load_json(source), origin=str(source))
    if isinstance(source, dict):
        return _validate_preset_dict(source, origin="<dict>")

    if isinstance(source, str):
        text = source.strip() or DEFAULT_PRESET_NAME

        # 1) inline-JSON прямо в строке
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise PresetError(
                    f"Строка пресета похожа на JSON, но не разбирается: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise PresetError(
                    "Inline-пресет должен быть JSON-объектом, "
                    f"получено: {type(data).__name__}"
                )
            return _validate_preset_dict(data, origin="<inline-json>")

        # 2) явный путь к файлу (есть расширение .json либо разделитель пути)
        looks_like_path = (
            text.lower().endswith(".json")
            or os.path.sep in text
            or "/" in text
            or ":" in text
        )
        if looks_like_path:
            candidate = Path(text)
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
            if not candidate.exists():
                raise PresetNotFoundError(f"Файл пресета не найден: {candidate}")
            return _validate_preset_dict(_load_json(candidate), origin=str(candidate))

        # 3) имя пресета в каталоге presets/
        path = preset_path(text)
        if not path.exists():
            if safe_preset_name(text) == DEFAULT_PRESET_NAME:
                ensure_default_preset()
            else:
                available = ", ".join(list_presets()) or "нет"
                raise PresetNotFoundError(
                    f"Пресет '{text}' не найден ({path}). "
                    f"Доступные пресеты: {available}."
                )
        return _validate_preset_dict(_load_json(path), origin=str(path))

    raise PresetError(
        "Неподдерживаемый тип источника пресета: "
        f"{type(source).__name__}. Ожидается StylePreset, str, Path или dict."
    )


def resolve_preset(
    preset: "StylePreset | str | Path | dict[str, Any] | None",
) -> StylePreset:
    """Псевдоним :func:`load_preset` — используется движком и экстрактором."""
    return load_preset(preset)


# --------------------------------------------------------------------------------------
# Публичный валидатор схемы пресета (для моста Document Studio и UI)
# --------------------------------------------------------------------------------------

#: Секции схемы пресета, поддержка которых подтверждается валидатором.
PRESET_SECTIONS: tuple[str, ...] = (
    "name", "description", "page", "body", "headings", "tables",
    "captions", "figures", "lists", "cleanup",
    # Новые блоки (ТЗ: форматирование титульного листа, спецразделов и колонтитулов).
    "title_page", "special_sections", "page_numbering",
)


def validate_preset_payload(data: Any) -> dict[str, Any]:
    """Проверяет словарь пресета по схеме и возвращает отчёт о валидации.

    Не бросает исключений: результат — словарь ``{"ok": bool, "errors": [...],
    "warnings": [...], "sections": [...]}``. Используется мостом Document Studio
    (валидация сохранённого пресета) и панелью инспектора, чтобы отличать
    поддерживаемые блоки схемы от неизвестных.
    """
    report: dict[str, Any] = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "sections": list(PRESET_SECTIONS),
    }
    if not isinstance(data, dict):
        report["errors"].append(
            "Пресет должен быть JSON-объектом, получено: "
            f"{type(data).__name__}"
        )
        return report

    # Предупреждения о неизвестных ключах верхнего уровня (не ошибка —
    # pydantic-модель использует extra='ignore').
    known = set(StylePreset.model_fields)
    for key in data:
        if key not in known:
            report["warnings"].append(f"Неизвестный блок '{key}' — будет проигнорирован")

    try:
        preset = StylePreset.model_validate(data)
    except ValidationError as exc:
        report["errors"] = [
            {
                "loc": list(err["loc"]),
                "path": ".".join(str(part) for part in err["loc"]) or "<root>",
                "message": err["msg"],
                "type": err["type"],
            }
            for err in exc.errors()
        ]
        return report

    report["ok"] = True
    report["name"] = preset.name
    report["summary"] = preset.summary()
    # Дополнительная семантическая проверка новых блоков.
    if preset.special_sections.enabled:
        unknown = [
            item for item in preset.special_sections.apply_to
            if item not in SPECIAL_SECTION_KINDS
        ]
        if unknown:
            report["warnings"].append(
                "special_sections.apply_to содержит неизвестные разделы: "
                + ", ".join(unknown)
            )
    if preset.page_numbering.enabled and preset.page_numbering.show_from_page < 1:
        report["warnings"].append("page_numbering.show_from_page должен быть >= 1")
    if (
        preset.title_page.enabled
        and preset.title_page.signatures.left_indent
        > preset.page.right + (210.0 - preset.page.left - preset.page.right)
    ):
        report["warnings"].append(
            "title_page.signatures.left_indent превышает ширину текста страницы"
        )
    return report
