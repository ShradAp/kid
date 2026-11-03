"""Content blocks and normalization independent of Word rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

try:
    from .preset_schema import Alignment, PresetError
except ImportError:  # Standalone module compatibility.
    from preset_schema import Alignment, PresetError


class DocxStructureError(PresetError):
    """Передана непонятная или пустая структура контента."""


_INLINE_MARK_RE = re.compile(
    r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|__[^_]+__|\*[^*\n]+\*|_[^_\n]+_)"
)


@dataclass(frozen=True)
class RunSpec:
    """Фрагмент текста с оформлением (для inline-разметки и явных ``runs``)."""

    text: str = ""
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None


@dataclass(frozen=True)
class ContentBlock:
    """Нормализованный блок контента (заголовок, абзац, список, таблица, подпись)."""

    kind: str
    level: int = 0
    text: str = ""
    runs: tuple[RunSpec, ...] = ()
    items: tuple[str, ...] = ()
    ordered: bool = False
    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    caption: str = ""
    caption_kind: str = "table"
    caption_number: Any = None
    alignment: Alignment | None = None
    bold: bool | None = None
    italic: bool | None = None
    first_line_indent_mm: float | None = None
    #: Сырые данные блока (для титульного листа и спецразделов).
    payload: dict[str, Any] = field(default_factory=dict)


#: Синонимы типов блоков -> канонический ``kind``.
BLOCK_TYPE_ALIASES: dict[str, str] = {
    "h1": "heading", "h2": "heading", "h3": "heading",
    "h4": "heading", "h5": "heading", "h6": "heading",
    "heading1": "heading", "heading2": "heading", "heading3": "heading",
    "heading_1": "heading", "heading_2": "heading", "heading_3": "heading",
    "heading": "heading", "title": "heading", "section": "heading",
    "chapter": "heading", "subsection": "heading", "subsubsection": "heading",
    "p": "paragraph", "paragraph": "paragraph", "text": "paragraph",
    "para": "paragraph", "body": "paragraph", "quote": "paragraph",
    "table": "table", "tbl": "table", "grid": "table",
    "list": "list", "ul": "list", "ol": "list", "bullet": "list",
    "bullets": "list", "numbered": "list", "ordered": "list",
    "caption": "caption", "figure_caption": "caption", "table_caption": "caption",
    "page_break": "page_break", "pagebreak": "page_break",
    "break": "page_break", "new_page": "page_break",
    # Титульный лист и специальные разделы (реферат/задание/содержание).
    "titlepage": "title_page", "title_page": "title_page", "cover": "title_page",
    "титульный": "title_page", "титульник": "title_page", "титул": "title_page",
    "task": "task", "zadanie": "task", "задание": "task",
    "referat": "referat", "abstract": "referat", "реферат": "referat",
    "content": "content", "toc": "content", "содержание": "content",
    "оглавление": "content", "contents": "content",
}

#: Типы блоков, которые рендерятся как специальные секции документа.
SPECIAL_BLOCK_KINDS: frozenset[str] = frozenset(
    {"title_page", "task", "referat", "content"}
)

#: Уровень заголовка по исходному имени типа.
_TYPE_TO_LEVEL: dict[str, int] = {
    "h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6,
    "heading1": 1, "heading2": 2, "heading3": 3,
    "heading_1": 1, "heading_2": 2, "heading_3": 3,
    "title": 1, "section": 1, "chapter": 1,
    "subsection": 2, "subsubsection": 3,
}

SUPPORTED_BLOCK_TYPES = (
    "h1, h2, h3 (заголовки); paragraph/p (абзац); list/ul/ol (список); "
    "table (таблица); caption (подпись); page_break (разрыв страницы)"
)


def _as_text(value: Any) -> str:
    """Аккуратно приводит значение к тексту (списки склеиваются через пробел)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(item) for item in value if item is not None)
    return str(value)


def _first_present(data: dict[str, Any], keys: Sequence[str]) -> Any:
    """Первое непустое значение среди перечисленных ключей словаря."""
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    """Приводит значение к ``bool``/``None`` (``None`` — «не переопределять»)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "да", "on", "вкл"):
        return True
    if text in ("0", "false", "no", "n", "нет", "off", "выкл"):
        return False
    return default


def _as_alignment(value: Any) -> Alignment | None:
    """Разбирает выравнивание из строки (в том числе русской)."""
    if value is None:
        return None
    if isinstance(value, Alignment):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    mapping = {
        "left": Alignment.LEFT, "l": Alignment.LEFT,
        "по левому": Alignment.LEFT, "слева": Alignment.LEFT,
        "левое": Alignment.LEFT,
        "center": Alignment.CENTER, "centre": Alignment.CENTER,
        "c": Alignment.CENTER, "по центру": Alignment.CENTER,
        "центр": Alignment.CENTER, "центральное": Alignment.CENTER,
        "right": Alignment.RIGHT, "r": Alignment.RIGHT,
        "по правому": Alignment.RIGHT, "справа": Alignment.RIGHT,
        "правое": Alignment.RIGHT,
        "justify": Alignment.JUSTIFY, "j": Alignment.JUSTIFY,
        "по ширине": Alignment.JUSTIFY, "justified": Alignment.JUSTIFY,
        "ширина": Alignment.JUSTIFY,
    }
    result = mapping.get(text)
    if result is None:
        try:
            result = Alignment(text)
        except ValueError:
            _log(f"word_automation: неизвестное выравнивание '{value}' — пропущено")
            return None
    return result


def _cell_text(value: Any) -> str:
    """Текст ячейки таблицы: ``None`` -> пустая строка, списки -> через '; '."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(_as_text(item) for item in value if item is not None)
    return _as_text(value)


def _pad_rows(
    rows: tuple[tuple[str, ...], ...], columns: int
) -> tuple[tuple[str, ...], ...]:
    """Дополняет строки до ``columns`` ячеек (защита от «рваных» таблиц)."""
    columns = max(1, columns)
    padded: list[tuple[str, ...]] = []
    for row in rows:
        if len(row) < columns:
            padded.append(row + ("",) * (columns - len(row)))
        else:
            padded.append(row[:columns])
    return tuple(padded)


def _normalize_rows(raw: Any, columns: int) -> tuple[tuple[str, ...], ...]:
    """Приводит строки таблицы к кортежам одинаковой ширины.

    Поддерживаются формы: список списков, список словарей, словарь колонок
    ``{"col": [...]}`` и плоский список (одно значение = одна строка из 1 ячейки).
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        keys = list(raw.keys())
        columns = max(columns, len(keys))
        height = 0
        for value in raw.values():
            if isinstance(value, (list, tuple)):
                height = max(height, len(value))
        result: list[tuple[str, ...]] = []
        for index in range(height):
            row: list[str] = []
            for key in keys:
                values = raw[key]
                if isinstance(values, (list, tuple)):
                    row.append(_cell_text(values[index]) if index < len(values) else "")
                else:
                    row.append(_cell_text(values) if index == 0 else "")
            result.append(tuple(row))
        return _pad_rows(tuple(result), columns)
    if isinstance(raw, (list, tuple)):
        rows: list[tuple[str, ...]] = []
        for item in raw:
            if isinstance(item, (list, tuple)):
                rows.append(tuple(_cell_text(cell) for cell in item))
            elif isinstance(item, dict):
                rows.append(tuple(_cell_text(value) for value in item.values()))
            else:
                rows.append((_cell_text(item),))
        width = max([columns] + [len(row) for row in rows])
        return _pad_rows(tuple(rows), width)
    raise DocxStructureError(
        "Поле rows таблицы должно быть списком списков/словарей, "
        f"получено: {type(raw).__name__}"
    )


def parse_inline_runs(text: str) -> tuple[RunSpec, ...]:
    """Разбирает inline-разметку ``**жирный**`` / ``*курсив*`` на фрагменты.

    Одиночные маркеры без пары остаются обычным текстом, поэтому разметка
    безопасна для текста вроде ``2 * 3`` или ``И.О. _Фамилия``.
    """
    source = _as_text(text)
    if not source or not _INLINE_MARK_RE.search(source):
        return (RunSpec(text=source),)
    specs: list[RunSpec] = []
    for part in _INLINE_MARK_RE.split(source):
        if not part:
            continue
        if part.startswith("***") and part.endswith("***") and len(part) > 6:
            specs.append(RunSpec(part[3:-3], bold=True, italic=True))
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            specs.append(RunSpec(part[2:-2], bold=True))
        elif part.startswith("__") and part.endswith("__") and len(part) > 4:
            specs.append(RunSpec(part[2:-2], bold=True))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            specs.append(RunSpec(part[1:-1], italic=True))
        elif part.startswith("_") and part.endswith("_") and len(part) > 2:
            specs.append(RunSpec(part[1:-1], italic=True))
        else:
            specs.append(RunSpec(part))
    return tuple(specs) or (RunSpec(text=source),)



# --------------------------------------------------------------------------------------
# Разбор структуры контента
# --------------------------------------------------------------------------------------


def _resolve_block_kind(data: dict[str, Any]) -> tuple[str, int]:
    """Определяет канонический тип блока и уровень заголовка.

    Возвращает ``(kind, level)``; ``level`` значим только для ``kind='heading'``.
    """
    raw_type = _first_present(data, ("type", "kind", "block", "block_type", "style"))
    if raw_type is None:
        # Тип не указан — угадываем по составу полей.
        if _first_present(data, ("rows", "data", "header", "headers", "columns")) is not None:
            return "table", 0
        if _first_present(data, ("items", "entries")) is not None:
            return "list", 0
        return "paragraph", 0

    text = str(raw_type).strip().lower().replace("-", "").replace(" ", "").replace("_", "")
    key = str(raw_type).strip().lower().replace("-", "").replace(" ", "")
    kind = BLOCK_TYPE_ALIASES.get(text) or BLOCK_TYPE_ALIASES.get(key)
    if kind is None:
        raise DocxStructureError(
            f"Неизвестный тип блока '{raw_type}'. Поддерживаются: {SUPPORTED_BLOCK_TYPES}"
        )
    level = _TYPE_TO_LEVEL.get(text) or _TYPE_TO_LEVEL.get(key) or 0
    return kind, level


def _explicit_runs(data: dict[str, Any]) -> tuple[RunSpec, ...]:
    """Явные фрагменты ``runs`` из блока (если заданы)."""
    raw = data.get("runs")
    if not isinstance(raw, (list, tuple)) or not raw:
        return ()
    specs: list[RunSpec] = []
    for item in raw:
        if isinstance(item, str):
            specs.append(RunSpec(text=item))
        elif isinstance(item, dict):
            specs.append(
                RunSpec(
                    text=_as_text(_first_present(item, ("text", "content", "value"))),
                    bold=_as_bool(item.get("bold")),
                    italic=_as_bool(item.get("italic")),
                    underline=_as_bool(item.get("underline")),
                )
            )
        else:
            specs.append(RunSpec(text=_as_text(item)))
    return tuple(spec for spec in specs if spec.text)


def _build_table_block(data: dict[str, Any]) -> ContentBlock:
    """Собирает блок таблицы из словаря, выравнивая ширину header/rows."""
    header_raw = _first_present(data, ("header", "headers", "columns", "thead"))
    header: tuple[str, ...] = ()
    if isinstance(header_raw, (list, tuple)):
        header = tuple(_cell_text(cell) for cell in header_raw)
    elif header_raw not in (None, ""):
        header = (_cell_text(header_raw),)
    rows = _normalize_rows(
        _first_present(data, ("rows", "data", "body", "tbody")), len(header)
    )
    if not header and not rows:
        raise DocxStructureError("Блок table пуст: передайте header и/или rows.")
    if header and rows and len(header) != len(rows[0]):
        width = max(len(header), len(rows[0]))
        header = _pad_rows((header,), width)[0]
        rows = _pad_rows(rows, width)
    return ContentBlock(
        kind="table",
        header=header,
        rows=rows,
        caption=_as_text(_first_present(data, ("caption", "title"))),
        caption_kind=str(
            _first_present(data, ("caption_type", "caption_kind")) or "table"
        ).strip().lower(),
        caption_number=_first_present(data, ("number", "caption_number", "n")),
        alignment=_as_alignment(_first_present(data, ("alignment", "align"))),
    )


def _build_list_block(data: dict[str, Any], kind_hint: str) -> ContentBlock:
    """Собирает блок списка из словаря (items либо текст по строкам)."""
    items_raw = _first_present(data, ("items", "entries", "values", "points"))
    if items_raw is None:
        text_value = _as_text(_first_present(data, ("text", "content", "value")))
        items_raw = [line for line in text_value.split("\n") if line.strip()]
    if isinstance(items_raw, (list, tuple)):
        items = tuple(_as_text(item).strip() for item in items_raw)
    else:
        items = (_as_text(items_raw).strip(),)
    items = tuple(item for item in items if item)
    if not items:
        raise DocxStructureError("Блок list пуст: передайте непустые items.")
    ordered = _as_bool(_first_present(data, ("ordered", "numbered", "numeric")))
    if ordered is None:
        ordered = kind_hint in ("ol", "ordered", "numbered")
    return ContentBlock(
        kind="list",
        items=items,
        ordered=bool(ordered),
        alignment=_as_alignment(_first_present(data, ("alignment", "align"))),
    )



def _build_caption_block(data: dict[str, Any]) -> ContentBlock:
    """Собирает блок подписи (рисунок или таблица)."""
    caption_kind = str(
        _first_present(data, ("caption_type", "caption_kind", "object")) or "figure"
    ).strip().lower()
    return ContentBlock(
        kind="caption",
        caption=_as_text(
            _first_present(data, ("text", "content", "value", "caption"))
        ),
        caption_kind=caption_kind,
        caption_number=_first_present(data, ("number", "caption_number", "n")),
        alignment=_as_alignment(_first_present(data, ("alignment", "align"))),
    )



def _build_special_block(data: dict[str, Any], kind: str) -> ContentBlock:
    """Собирает блок титульного листа или специального раздела.

    ``title_page`` хранит сырой словарь (шапка/заголовок/подписи/подвал).
    ``task``/``referat``/``content`` — заголовок раздела, абзацы тела и пункты
    списка (маркеры не переформатируются, чтобы сохранить дефисы и буквы).
    """
    payload = dict(data)
    if kind == "title_page":
        return ContentBlock(kind="title_page", payload=payload)

    title = _as_text(_first_present(data, ("title", "heading", "name", "text")))
    body: list[str] = []
    raw_body = _first_present(data, ("body", "paragraphs", "lines", "value", "content"))
    if isinstance(raw_body, (list, tuple)):
        body = [_as_text(item).strip() for item in raw_body if _as_text(item).strip()]
    elif raw_body not in (None, ""):
        body = [line.strip() for line in _as_text(raw_body).split("\n") if line.strip()]

    items: list[str] = []
    raw_items = _first_present(data, ("items", "entries", "points", "tasks", "list"))
    if isinstance(raw_items, (list, tuple)):
        items = [_as_text(item).strip() for item in raw_items if _as_text(item).strip()]
    elif raw_items not in (None, ""):
        items = [line.strip() for line in _as_text(raw_items).split("\n") if line.strip()]

    payload["_title"] = title
    payload["_body"] = tuple(body)
    payload["_items"] = tuple(items)
    payload["_ordered"] = bool(
        _as_bool(_first_present(data, ("ordered", "numbered", "numeric")))
    )
    return ContentBlock(
        kind=kind, text=title, items=tuple(items), payload=payload
    )


def _block_from_dict(data: dict[str, Any], inherited_level: int = 0) -> list[ContentBlock]:
    """Собирает блок (и его вложенных детей) из словаря."""
    kind, level = _resolve_block_kind(data)
    blocks: list[ContentBlock] = []

    if kind == "table":
        blocks.append(_build_table_block(data))
    elif kind == "list":
        hint = str(
            _first_present(data, ("type", "kind", "block")) or ""
        ).strip().lower()
        blocks.append(_build_list_block(data, hint))
    elif kind == "caption":
        blocks.append(_build_caption_block(data))
    elif kind == "page_break":
        blocks.append(ContentBlock(kind="page_break"))
    elif kind in SPECIAL_BLOCK_KINDS:
        blocks.append(_build_special_block(data, kind))
    elif kind == "heading":
        text = _as_text(
            _first_present(data, ("text", "content", "value", "title"))
        ).strip()
        if not text and not _explicit_runs(data):
            raise DocxStructureError(f"Блок заголовка (уровень {level}) без текста.")
        if not level:
            level = int(data.get("level") or inherited_level or 1)
        level = max(1, min(9, level))
        blocks.append(
            ContentBlock(
                kind="heading",
                level=level,
                text=text,
                runs=_explicit_runs(data),
                alignment=_as_alignment(_first_present(data, ("alignment", "align"))),
                bold=_as_bool(data.get("bold")),
                italic=_as_bool(data.get("italic")),
            )
        )
    else:  # paragraph
        text = _as_text(_first_present(data, ("text", "content", "value"))).strip()
        if not text and not _explicit_runs(data):
            raise DocxStructureError("Блок paragraph без текста.")
        indent = _first_present(
            data, ("first_line_indent", "first_line_indent_mm", "indent")
        )
        blocks.append(
            ContentBlock(
                kind="paragraph",
                text=text,
                runs=_explicit_runs(data),
                alignment=_as_alignment(_first_present(data, ("alignment", "align"))),
                bold=_as_bool(data.get("bold")),
                italic=_as_bool(data.get("italic")),
                first_line_indent_mm=float(indent) if indent is not None else None,
            )
        )

    # Вложенные дети разворачиваются сразу после самого блока.
    children = _first_present(
        data, ("children", "blocks", "content_blocks", "subblocks", "sub")
    )
    if isinstance(children, (list, tuple)) and children:
        child_level = level + 1 if kind == "heading" else inherited_level
        for child in children:
            blocks.extend(_normalize_item(child, child_level))
    return blocks


def _normalize_item(item: Any, inherited_level: int = 0) -> list[ContentBlock]:
    """Приводит один элемент структуры к списку блоков."""
    if item is None:
        return []
    if isinstance(item, ContentBlock):
        return [item]
    if isinstance(item, dict):
        return _block_from_dict(item, inherited_level)
    if isinstance(item, (list, tuple)):
        result: list[ContentBlock] = []
        for child in item:
            result.extend(_normalize_item(child, inherited_level))
        return result
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return []
        return [ContentBlock(kind="paragraph", text=text)]
    raise DocxStructureError(
        "Элемент структуры должен быть строкой, словарём или списком, "
        f"получено: {type(item).__name__}"
    )


def normalize_structure(structure: Any) -> list[ContentBlock]:
    """Превращает древовидную/списочную структуру в плоский список блоков.

    Поддерживаются:
      * ``list`` блоков;
      * ``dict`` с ключом ``blocks``/``content``/``children`` (и необязательным
        ``title`` — он становится заголовком h1);
      * одиночный блок-словарь, строка, вложенные списки любой глубины.
    """
    if structure is None:
        raise DocxStructureError("Передана пустая структура контента (None).")

    if isinstance(structure, dict):
        blocks: list[ContentBlock] = []
        title = _first_present(structure, ("title", "document_title", "doc_title"))
        if title not in (None, ""):
            blocks.append(ContentBlock(kind="heading", level=1, text=_as_text(title)))
        body = _first_present(
            structure,
            ("blocks", "content", "children", "content_blocks", "sections", "items"),
        )
        known_type = _first_present(structure, ("type", "kind", "block", "block_type"))
        if body is not None and known_type is None:
            for child in _as_list(body):
                blocks.extend(_normalize_item(child, 1))
            if not blocks:
                raise DocxStructureError(
                    "Структура документа пуста: ни title, ни blocks не дали контента."
                )
            return blocks
        # Словарь описывает один блок (возможно, с детьми).
        blocks.extend(_block_from_dict(structure))
        return blocks

    blocks = []
    for child in _as_list(structure):
        blocks.extend(_normalize_item(child))
    if not blocks:
        raise DocxStructureError(
            "Структура документа пуста: передан пустой список или только пустые строки."
        )
    return blocks


def _as_list(value: Any) -> list[Any]:
    """Гарантирует список для итерации (строка не разворачивается в символы)."""
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]

