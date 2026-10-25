"""Всеобъемлющий анализ оформления существующего DOCX (python-docx).

Модуль читает загруженный ``.docx`` и извлекает из него полный набор правил
оформления, который затем ложится в :class:`StylePreset` и наполняет правую
панель Document Studio:

* поля страницы (верхнее/нижнее/левое/правое) и ориентация;
* параметры базового абзаца (интервалы до/после, межстрочный интервал,
  отступ первой строки, выравнивание);
* параметры шрифта основного текста, заголовков (h1-h3) и подписей
  (гарнитура, кегль, полужирный/курсив);
* свойства таблиц (выравнивание на странице, шрифт/кегль в ячейках,
  отступы внутри ячеек);
* выравнивание графических элементов и их подписей;
* параметры отступов списков.

Дополнительно :func:`build_preview` собирает плоскую модель документа для
центрального предпросмотра: абзацы, заголовки, таблицы, списки, подписи и
рисунки (с картинками в виде data-URL) в порядке следования в файле.
"""

from __future__ import annotations

import base64
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import docx
from docx.document import Document as DocumentObject
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Emu, Length
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

try:  # импорт внутри пакета
    from .preset_schema import (
        Alignment,
        BodyStyle,
        CaptionPlacement,
        CaptionStyle,
        CellVerticalAlignment,
        CleanupPolicy,
        FigureStyle,
        HeadingStyle,
        LineSpacingRule,
        ListStyle,
        Orientation,
        PageNumberingStyle,
        PageSettings,
        SpecialSectionsStyle,
        StylePreset,
        TableStyle,
        TitlePageStyle,
        _log,
        default_gost_preset,
    )
    try:  # общие хелперы распознавания секций живут в движке
        from .docx_engine import (
            _classify_document_sections,
            _footer_start_index,
            _matches_any,
            _paragraph_has_underline,
            _section_heading_level,
            _structural_titles,
            _upper_ratio,
        )
    except ImportError:  # pragma: no cover
        from docx_engine import (  # type: ignore[no-redef]
            _classify_document_sections,
            _footer_start_index,
            _matches_any,
            _paragraph_has_underline,
            _section_heading_level,
            _structural_titles,
            _upper_ratio,
        )
except ImportError:  # pragma: no cover - запуск модуля вне пакета
    from preset_schema import (  # type: ignore[no-redef]
        Alignment,
        BodyStyle,
        CaptionPlacement,
        CaptionStyle,
        CellVerticalAlignment,
        CleanupPolicy,
        FigureStyle,
        HeadingStyle,
        LineSpacingRule,
        ListStyle,
        Orientation,
        PageNumberingStyle,
        PageSettings,
        SpecialSectionsStyle,
        StylePreset,
        TableStyle,
        TitlePageStyle,
        _log,
        default_gost_preset,
    )
    from docx_engine import (  # type: ignore[no-redef]
        _classify_document_sections,
        _footer_start_index,
        _matches_any,
        _paragraph_has_underline,
        _section_heading_level,
        _structural_titles,
        _upper_ratio,
    )


# --------------------------------------------------------------------------------------
# Соответствия Word -> пресет
# --------------------------------------------------------------------------------------

_ALIGN_TO_ENUM: dict[Any, Alignment] = {
    WD_ALIGN_PARAGRAPH.LEFT: Alignment.LEFT,
    WD_ALIGN_PARAGRAPH.CENTER: Alignment.CENTER,
    WD_ALIGN_PARAGRAPH.RIGHT: Alignment.RIGHT,
    WD_ALIGN_PARAGRAPH.JUSTIFY: Alignment.JUSTIFY,
    WD_ALIGN_PARAGRAPH.DISTRIBUTE: Alignment.JUSTIFY,
}

_TABLE_ALIGN_TO_ENUM: dict[Any, Alignment] = {
    WD_TABLE_ALIGNMENT.LEFT: Alignment.LEFT,
    WD_TABLE_ALIGNMENT.CENTER: Alignment.CENTER,
    WD_TABLE_ALIGNMENT.RIGHT: Alignment.RIGHT,
}

_RULE_TO_ENUM: dict[Any, LineSpacingRule] = {
    WD_LINE_SPACING.SINGLE: LineSpacingRule.SINGLE,
    WD_LINE_SPACING.ONE_POINT_FIVE: LineSpacingRule.ONE_POINT_FIVE,
    WD_LINE_SPACING.DOUBLE: LineSpacingRule.DOUBLE,
    WD_LINE_SPACING.EXACTLY: LineSpacingRule.EXACTLY,
    WD_LINE_SPACING.AT_LEAST: LineSpacingRule.AT_LEAST,
    WD_LINE_SPACING.MULTIPLE: LineSpacingRule.MULTIPLE,
}

#: Точки на миллиметр.
_PT_PER_MM = 72.0 / 25.4
#: Твипсов (dxa) на миллиметр.
_TWIPS_PER_MM = 1440.0 / 25.4

#: Признаки подписи («Таблица 1 — ...», «Рисунок 2 — ...», «Рис. 3»).
_CAPTION_RE = re.compile(
    r"^(таблиц|рисунок|рис\.|figure|table|приложени|appendix|схема|чертёж|чертеж)"
    r"[а-яa-z]*\s*(№|\d|[А-ЯA-Z.])",
    re.IGNORECASE,
)
#: Ведущий маркер элемента списка: «1.», «1)», «а)», «–», «•».
_LIST_MARKER_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]|[а-яa-z][.)]|[–—•·*o-])\s+\S", re.IGNORECASE
)


def _mm_from_length(value: Any) -> float | None:
    """Переводит ``Length``/EMU/твипсы в миллиметры (``None`` — не задано)."""
    if value is None:
        return None
    if isinstance(value, Length):
        return round(value.mm, 2)
    if isinstance(value, (int, float)):
        # python-docx хранит длину в EMU; 1 мм = 36000 EMU.
        return round(float(value) / 36000.0, 2)
    return None


def _pt_from_length(value: Any) -> float | None:
    """Переводит ``Length`` в пункты (``None`` — не задано)."""
    if value is None:
        return None
    if isinstance(value, Length):
        return round(value.pt, 2)
    if isinstance(value, (int, float)):
        return round(float(value) / 12700.0, 2)
    return None


def _dxa_to_mm(value: Any) -> float | None:
    """Твипсы (``w:w`` со ``w:type='dxa'``) в миллиметры."""
    try:
        return round(float(value) / _TWIPS_PER_MM, 3)
    except (TypeError, ValueError):
        return None


def _mode(values: Iterable[Any]) -> Any:
    """Наиболее частое значение среди непустых (``None`` отбрасывается)."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    counted = Counter(clean)
    return counted.most_common(1)[0][0]


# --------------------------------------------------------------------------------------
# Разбор абзаца
# --------------------------------------------------------------------------------------


def _style_name(paragraph: Paragraph) -> str:
    """Имя стиля абзаца (пустая строка, если недоступно)."""
    try:
        return str(paragraph.style.name or "")
    except (AttributeError, KeyError):  # noqa: PERF203
        return ""


def _default_font(document: DocumentObject) -> tuple[str, float | None]:
    """Гарнитура/кегль стиля ``Normal`` — резервный источник шрифта."""
    try:
        normal = document.styles["Normal"].font
        name = normal.name or "Times New Roman"
        return str(name), _pt_from_length(normal.size)
    except (KeyError, ValueError, AttributeError):
        return "Times New Roman", None


def _classify_paragraph(paragraph: Paragraph) -> tuple[str, int]:
    """Тип абзаца (``body``/``heading``/``caption``/``list``) и уровень заголовка."""
    lowered = _style_name(paragraph).lower()
    if lowered.startswith(("heading", "заголовок")):
        digits = "".join(ch for ch in _style_name(paragraph) if ch.isdigit())
        return "heading", int(digits) if digits else 1
    if lowered in ("title", "subtitle", "название"):
        return "heading", 1
    if lowered in ("caption", "подпись"):
        return "caption", 0
    if lowered.startswith(("list", "список")):
        return "list", 0
    text = paragraph.text.strip()
    if _CAPTION_RE.match(text):
        return "caption", 0
    if text[:2] in ("- ", "– ", "* ", "• ") or _LIST_MARKER_RE.match(text):
        return "list", 0
    return "body", 0


def _effective_alignment(paragraph: Paragraph) -> Alignment | None:
    """Выравнивание абзаца с учётом наследования от стиля."""
    for pf in (
        paragraph.paragraph_format,
        getattr(paragraph.style, "paragraph_format", None),
    ):
        if pf is None:
            continue
        value = pf.alignment
        if value is not None:
            converted = _ALIGN_TO_ENUM.get(value)
            if converted is not None:
                return converted
    return None


def _line_spacing(paragraph: Paragraph) -> tuple[float | None, LineSpacingRule | None]:
    """Межстрочный интервал абзаца: (значение, правило)."""
    for pf in (
        paragraph.paragraph_format,
        getattr(paragraph.style, "paragraph_format", None),
    ):
        if pf is None:
            continue
        rule = pf.line_spacing_rule
        value = pf.line_spacing
        if value is None and rule is None:
            continue
        if isinstance(value, Length):
            return round(value.pt, 2), _RULE_TO_ENUM.get(rule, LineSpacingRule.EXACTLY)
        if value is not None:
            return round(float(value), 2), _RULE_TO_ENUM.get(rule, LineSpacingRule.MULTIPLE)
    return None, None


def _paragraph_font(
    paragraph: Paragraph, document: DocumentObject
) -> dict[str, Any]:
    """Шрифт абзаца: гарнитура, кегль, полужирный, курсив (по «смысловым» ранам)."""
    default_name, default_size = _default_font(document)
    names: list[str] = []
    sizes: list[float] = []
    bolds: list[bool] = []
    italics: list[bool] = []
    for run in paragraph.runs:
        if not run.text.strip():
            continue
        font = run.font
        name = font.name or default_name
        names.append(str(name))
        size = _pt_from_length(font.size)
        if size is None:
            size = default_size
        sizes.append(size)
        bolds.append(bool(font.bold))
        italics.append(bool(font.italic))
    if not names:
        # Абзац без ран: берём параметры стиля абзаца.
        try:
            style_font = paragraph.style.font
            return {
                "font_name": str(style_font.name or default_name),
                "font_size": _pt_from_length(style_font.size) or default_size,
                "bold": bool(style_font.bold),
                "italic": bool(style_font.italic),
            }
        except (KeyError, AttributeError):
            return {
                "font_name": default_name,
                "font_size": default_size,
                "bold": None,
                "italic": None,
            }
    return {
        "font_name": _mode(names) or default_name,
        "font_size": _mode(sizes),
        "bold": (lambda v: None if v is None else bool(v))( _mode(bolds)),
        "italic": (lambda v: None if v is None else bool(v))(_mode(italics)),
    }


def _pf_metric(paragraph: Paragraph, attr: str) -> Any:
    """Значение атрибута paragraph_format с наследованием от стиля."""
    for pf in (
        paragraph.paragraph_format,
        getattr(paragraph.style, "paragraph_format", None),
    ):
        if pf is None:
            continue
        value = getattr(pf, attr, None)
        if value is not None:
            return value
    return None


#: Пресет с ключевыми словами структурных разделов — нужен для распознавания
#: заголовков ПО ТЕКСТУ, когда собственный пресет ещё не построен.
_KEYWORD_PRESET: StylePreset | None = None


def _keyword_preset() -> StylePreset:
    """Лениво создаёт пресет-эталон с ключевыми словами разделов."""
    global _KEYWORD_PRESET
    if _KEYWORD_PRESET is None:
        _KEYWORD_PRESET = default_gost_preset()
    return _KEYWORD_PRESET


def _text_heading_level(paragraph: Paragraph, text: str) -> int | None:
    """Уровень заголовка раздела, определённый ПО ТЕКСТУ (1..9) или ``None``.

    В реальных ВКР и рефератах структурные заголовки («ВВЕДЕНИЕ», «РЕФЕРАТ»,
    «1 ГЛАВА. АНАЛИЗ ПРЕДМЕТНОЙ ОБЛАСТИ») обычно оформлены ВРУЧНУЮ: стиль
    «Normal», но кегль крупнее основного текста, полужирное начертание и
    выравнивание по центру. По имени стиля они не распознаются, поэтому иерархия
    кеглей не извлекалась, а генератор подменял заголовки стилем основного текста.

    Нумерованный заголовок принимается только при сильном признаке оформления —
    верхнем регистре или полужирном начертании. Иначе обычный абзац вида
    «3 способа решения задачи» ошибочно стал бы заголовком первого уровня.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    reference = _keyword_preset()
    level = _section_heading_level(stripped, reference)
    if level is None:
        return None
    if _matches_any(stripped, _structural_titles(reference)):
        return level
    strong = _upper_ratio(stripped) >= 0.6 or any(
        bool(run.bold) for run in paragraph.runs if run.text.strip()
    )
    return level if strong else None


def _collect_group(
    paragraphs: list[Paragraph], document: DocumentObject
) -> dict[str, Any]:
    """Собирает усреднённые метрики группы абзацев (мода по каждому параметру)."""
    before: list[float] = []
    after: list[float] = []
    spacing: list[float] = []
    rules: list[LineSpacingRule] = []
    indents: list[float] = []
    aligns: list[Alignment] = []
    fonts: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        b = _pt_from_length(_pf_metric(paragraph, "space_before"))
        a = _pt_from_length(_pf_metric(paragraph, "space_after"))
        if b is not None:
            before.append(b)
        if a is not None:
            after.append(a)
        ls_value, ls_rule = _line_spacing(paragraph)
        if ls_value is not None:
            spacing.append(ls_value)
        if ls_rule is not None:
            rules.append(ls_rule)
        indent = _mm_from_length(_pf_metric(paragraph, "first_line_indent"))
        if indent is not None:
            indents.append(indent)
        alignment = _effective_alignment(paragraph)
        if alignment is not None:
            aligns.append(alignment)
        fonts.append(_paragraph_font(paragraph, document))
    return {
        "space_before": _mode(before),
        "space_after": _mode(after),
        "line_spacing": _mode(spacing),
        "line_spacing_rule": _mode(rules),
        "first_line_indent": _mode(indents),
        "alignment": _mode(aligns),
        "font_name": _mode([f.get("font_name") for f in fonts]),
        "font_size": _mode([f.get("font_size") for f in fonts]),
        "bold": _mode([f.get("bold") for f in fonts]),
        "italic": _mode([f.get("italic") for f in fonts]),
    }


def _table_cell_margins(table: Table) -> dict[str, float]:
    """Отступы внутри ячеек из ``w:tblCellMar`` (мм)."""
    result: dict[str, float] = {}
    try:
        tblPr = table._tbl.tblPr
    except AttributeError:  # pragma: no cover - защита от нестандартного XML
        return result
    mar = tblPr.find(qn("w:tblCellMar"))
    if mar is None:
        return result
    for side in ("top", "bottom", "left", "right"):
        element = mar.find(qn(f"w:{side}"))
        if element is None:
            continue
        value = _dxa_to_mm(element.get(qn("w:w")))
        if value is not None:
            result[side] = value
    return result


def _collect_tables(document: DocumentObject) -> dict[str, Any]:
    """Собирает свойства таблиц: выравнивание, шрифт/кегль ячеек, отступы."""
    table_aligns: list[Alignment] = []
    names: list[str] = []
    sizes: list[float] = []
    italics: list[bool] = []
    header_bolds: list[bool] = []
    margins: dict[str, list[float]] = {"top": [], "bottom": [], "left": [], "right": []}
    for table in document.tables:
        align = _TABLE_ALIGN_TO_ENUM.get(table.alignment)
        if align is not None:
            table_aligns.append(align)
        for side, value in _table_cell_margins(table).items():
            margins[side].append(value)
        for row_index, row in enumerate(table.rows):
            is_header = row_index == 0
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    font = _paragraph_font(paragraph, document)
                    if font.get("font_name"):
                        names.append(font["font_name"])
                    if font.get("font_size"):
                        sizes.append(font["font_size"])
                    if font.get("italic") is not None:
                        italics.append(bool(font["italic"]))
                    if is_header:
                        header_bolds.append(bool(font.get("bold")))
    return {
        "table_alignment": _mode(table_aligns),
        "font_name": _mode(names),
        "font_size": _mode(sizes),
        "italic": _mode(italics),
        "header_bold": _mode(header_bolds),
        "cell_margin_top": _mode(margins["top"]),
        "cell_margin_bottom": _mode(margins["bottom"]),
        "cell_margin_left": _mode(margins["left"]),
        "cell_margin_right": _mode(margins["right"]),
    }


def _paragraph_has_image(p_el: Any) -> bool:
    """Есть ли в XML абзаца рисунок."""
    return p_el.find(".//" + qn("a:blip")) is not None or p_el.find(
        ".//" + qn("w:drawing")
    ) is not None


def _collect_figures(document: DocumentObject) -> Alignment | None:
    """Выравнивание абзацев, содержащих рисунки."""
    aligns: list[Alignment] = []
    for paragraph in document.paragraphs:
        if _paragraph_has_image(paragraph._p):
            alignment = _effective_alignment(paragraph)
            if alignment is not None:
                aligns.append(alignment)
    return _mode(aligns)


def _collect_lists(document: DocumentObject) -> dict[str, Any]:
    """Отступы и выравнивание элементов списка."""
    indents: list[float] = []
    aligns: list[Alignment] = []
    for paragraph in document.paragraphs:
        kind, _level = _classify_paragraph(paragraph)
        if kind != "list":
            continue
        indent = _mm_from_length(_pf_metric(paragraph, "left_indent"))
        if indent is not None:
            indents.append(indent)
        alignment = _effective_alignment(paragraph)
        if alignment is not None:
            aligns.append(alignment)
    return {"left_indent": _mode(indents), "alignment": _mode(aligns)}


# --------------------------------------------------------------------------------------
# Модель предпросмотра (для центрального холста студии)
# --------------------------------------------------------------------------------------


def _paragraph_images(p_el: Any, document: DocumentObject) -> list[dict[str, Any]]:
    """Возвращает рисунки абзаца как data-URL с размерами в мм."""
    images: list[dict[str, Any]] = []
    width_mm = height_mm = None
    extent = p_el.find(".//" + qn("wp:extent"))
    if extent is not None:
        try:
            width_mm = round(int(extent.get("cx")) / 36000.0, 2)
            height_mm = round(int(extent.get("cy")) / 36000.0, 2)
        except (TypeError, ValueError):
            width_mm = height_mm = None
    for blip in p_el.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if not rid:
            continue
        try:
            part = document.part.related_parts[rid]
            blob = part.blob
            content_type = part.content_type
        except (KeyError, AttributeError, ValueError):
            continue
        encoded = base64.b64encode(blob).decode("ascii")
        images.append(
            {
                "type": "image",
                "dataUrl": f"data:{content_type};base64,{encoded}",
                "widthMm": width_mm,
                "heightMm": height_mm,
            }
        )
    return images


def _paragraph_block(paragraph: Paragraph, document: DocumentObject) -> dict[str, Any]:
    """Формирует базовые параметры блока предпросмотра из абзаца."""
    kind, level = _classify_paragraph(paragraph)
    font = _paragraph_font(paragraph, document)
    ls_value, ls_rule = _line_spacing(paragraph)
    alignment = _effective_alignment(paragraph)
    return {
        "kind": kind,
        "level": level,
        "alignment": alignment.value if alignment else None,
        "fontName": font.get("font_name"),
        "fontSizePt": font.get("font_size"),
        "bold": font.get("bold"),
        "italic": font.get("italic"),
        "lineSpacing": ls_value,
        "lineSpacingRule": ls_rule.value if ls_rule else None,
        "firstLineIndentMm": _mm_from_length(_pf_metric(paragraph, "first_line_indent")),
        "spaceBeforePt": _pt_from_length(_pf_metric(paragraph, "space_before")),
        "spaceAfterPt": _pt_from_length(_pf_metric(paragraph, "space_after")),
    }


def _table_block(table: Table, document: DocumentObject) -> dict[str, Any]:
    """Формирует блок таблицы для предпросмотра."""
    rows = [[cell.text for cell in row.cells] for row in table.rows]
    align = _TABLE_ALIGN_TO_ENUM.get(table.alignment)
    font: dict[str, Any] = {}
    if table.rows and table.rows[0].cells:
        cell = table.rows[0].cells[0]
        if cell.paragraphs:
            font = _paragraph_font(cell.paragraphs[0], document)
    margins = _table_cell_margins(table)
    return {
        "type": "table",
        "rows": rows,
        "tableAlignment": align.value if align else None,
        "fontName": font.get("font_name"),
        "fontSizePt": font.get("font_size"),
        "cellMarginTopMm": margins.get("top"),
        "cellMarginBottomMm": margins.get("bottom"),
        "cellMarginLeftMm": margins.get("left"),
        "cellMarginRightMm": margins.get("right"),
    }


def build_preview(
    document: DocumentObject, *, include_images: bool = True
) -> dict[str, Any]:
    """Строит плоскую модель документа в порядке следования элементов."""
    blocks: list[dict[str, Any]] = []
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:tbl"):
            blocks.append(_table_block(Table(child, document), document))
            continue
        if child.tag != qn("w:p"):
            continue
        paragraph = Paragraph(child, document)
        if include_images:
            for image in _paragraph_images(child, document):
                image["alignment"] = (
                    _effective_alignment(paragraph).value
                    if _effective_alignment(paragraph)
                    else None
                )
                blocks.append(image)
        text = paragraph.text
        if not text.strip():
            continue
        block = _paragraph_block(paragraph, document)
        block["type"] = "heading" if block["kind"] == "heading" else block["kind"]
        if block["type"] in ("body", "paragraph"):
            block["type"] = "paragraph"
        block["text"] = text
        blocks.append(block)
    return {"blocks": blocks}


# --------------------------------------------------------------------------------------
# Сборка пресета из документа
# --------------------------------------------------------------------------------------


def _pick(value: Any, default: Any) -> Any:
    """Возвращает ``value``, если оно задано, иначе ``default``."""
    return default if value is None else value


def _updates(source: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """Отфильтровывает пары ключ/значение, где значение не ``None``."""
    return {key: source[key] for key in keys if source.get(key) is not None}


def _build_title_page_style(
    document: DocumentObject, paragraphs: list[Paragraph], base: "TitlePageStyle"
) -> "TitlePageStyle":
    """Собирает конфиг титульного листа из распознанных абзацев документа."""
    footer_start = _footer_start_index(paragraphs)
    header_ps: list[Paragraph] = []
    title_ps: list[Paragraph] = []
    sig_ps: list[Paragraph] = []
    foot_ps: list[Paragraph] = []
    for index, paragraph in enumerate(paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue
        if footer_start is not None and index >= footer_start:
            foot_ps.append(paragraph)
        elif _matches_any(text, base.signature_keywords) or _paragraph_has_underline(
            paragraph
        ):
            sig_ps.append(paragraph)
        elif _matches_any(text, base.title_keywords):
            title_ps.append(paragraph)
        else:
            header_ps.append(paragraph)

    keys = ("font_name", "font_size", "bold", "italic", "alignment", "space_after")

    def _apply(group: list[Paragraph], target: Any) -> Any:
        if not group:
            return target
        return target.model_copy(update=_updates(_collect_group(group, document), keys))

    header = _apply(header_ps, base.header)
    title = _apply(title_ps, base.title)
    signatures = _apply(sig_ps, base.signatures)
    footer = _apply(foot_ps, base.footer)
    if sig_ps:
        left = _mode(
            [_mm_from_length(_pf_metric(p, "left_indent")) for p in sig_ps]
        )
        if left is not None:
            signatures = signatures.model_copy(update={"left_indent": left})
    return base.model_copy(
        update={
            "enabled": True,
            "header": header,
            "title": title,
            "signatures": signatures,
            "footer": footer,
        }
    )


def _build_special_sections_style(
    document: DocumentObject, sections: dict[str, list[Paragraph]], base: "SpecialSectionsStyle"
) -> "SpecialSectionsStyle":
    """Собирает конфиг спецразделов из распознанных разделов документа."""
    result = base.model_copy()
    for _kind, paragraphs in sections.items():
        nonempty = [p for p in paragraphs if p.text.strip()]
        if not nonempty:
            continue
        title_m = _collect_group([nonempty[0]], document)
        body_m = _collect_group(nonempty[1:], document) if len(nonempty) > 1 else {}
        update: dict[str, Any] = {}
        for src, dst in (
            ("font_name", "title_font_name"), ("font_size", "title_font_size"),
            ("bold", "title_bold"), ("italic", "title_italic"),
            ("alignment", "title_alignment"),
            ("first_line_indent", "title_first_line_indent"),
            ("space_after", "title_space_after"),
        ):
            if title_m.get(src) is not None:
                update[dst] = title_m[src]
        for src, dst in (
            ("font_name", "body_font_name"), ("font_size", "body_font_size"),
            ("alignment", "body_alignment"),
            ("first_line_indent", "body_first_line_indent"),
            ("line_spacing", "body_line_spacing"),
            ("line_spacing_rule", "body_line_spacing_rule"),
        ):
            if body_m.get(src) is not None:
                update[dst] = body_m[src]
        if update:
            result = result.model_copy(update=update)
    return result


def _detect_page_numbering(
    document: DocumentObject, base: "PageNumberingStyle"
) -> "PageNumberingStyle":
    """Определяет, есть ли в документе нумерация страниц в колонтитуле."""
    enabled = False
    alignment = base.alignment
    start_at = base.start_at
    for section in document.sections:
        sectPr = section._sectPr
        pg = sectPr.find(qn("w:pgNumType"))
        if pg is not None and pg.get(qn("w:start")):
            try:
                start_at = int(pg.get(qn("w:start")))
            except (TypeError, ValueError):
                pass
        for paragraph in section.footer.paragraphs:
            instrs = paragraph._p.findall(".//" + qn("w:instrText"))
            if any("PAGE" in (el.text or "") for el in instrs):
                enabled = True
                detected = _effective_alignment(paragraph)
                if detected is not None:
                    alignment = detected
    if not enabled:
        return base
    return base.model_copy(
        update={"enabled": True, "alignment": alignment, "start_at": start_at}
    )


def _build_preset(
    document: DocumentObject,
    name: str,
    description: str,
    report: dict[str, Any],
) -> StylePreset:
    """Собирает :class:`StylePreset` из распознанного оформления документа."""
    base = default_gost_preset()

    # --- поля страницы
    page = base.page
    if document.sections:
        section = document.sections[0]
        page = PageSettings(
            top=_pick(_mm_from_length(section.top_margin), base.page.top),
            bottom=_pick(_mm_from_length(section.bottom_margin), base.page.bottom),
            left=_pick(_mm_from_length(section.left_margin), base.page.left),
            right=_pick(_mm_from_length(section.right_margin), base.page.right),
            orientation=(
                Orientation.LANDSCAPE
                if section.orientation == WD_ORIENT.LANDSCAPE
                else Orientation.PORTRAIT
            ),
        )
    report["page"] = {
        "top": page.top,
        "bottom": page.bottom,
        "left": page.left,
        "right": page.right,
        "orientation": page.orientation.value,
    }

    # --- разбор абзацев по типам
    body_ps: list[Paragraph] = []
    heading_ps: dict[int, list[Paragraph]] = {}
    caption_ps: list[Paragraph] = []
    list_ps: list[Paragraph] = []
    for paragraph in document.paragraphs:
        kind, level = _classify_paragraph(paragraph)
        if kind == "heading":
            heading_ps.setdefault(max(1, min(level, 9)), []).append(paragraph)
        elif kind == "caption":
            caption_ps.append(paragraph)
        elif kind == "list":
            list_ps.append(paragraph)
        else:
            # Заголовок раздела, оформленный ВРУЧНУЮ (стиль «Normal», но крупный
            # кегль, полужирное начертание и выравнивание по центру), по имени
            # стиля не распознаётся. Определяем его по тексту, иначе иерархия
            # кеглей не извлекалась из входного файла, а генератор подменял
            # структурные заголовки профилем основного текста.
            text_level = _text_heading_level(paragraph, paragraph.text)
            if text_level is not None:
                heading_ps.setdefault(text_level, []).append(paragraph)
            else:
                body_ps.append(paragraph)

    body_m = _collect_group(body_ps, document) if body_ps else {}
    body = base.body.model_copy(
        update=_updates(
            body_m,
            (
                "font_name", "font_size", "alignment", "line_spacing",
                "line_spacing_rule", "first_line_indent", "space_before", "space_after",
            ),
        )
    )

    headings = dict(base.headings)
    for level, paragraphs in heading_ps.items():
        metrics = _collect_group(paragraphs, document)
        key = f"h{level}"
        current = headings.get(key) or base.heading(level)
        headings[key] = current.model_copy(
            update=_updates(
                metrics,
                (
                    "font_name", "font_size", "bold", "italic", "alignment",
                    "space_before", "space_after", "first_line_indent",
                ),
            )
        )

    caption_m = _collect_group(caption_ps, document) if caption_ps else {}
    captions = base.captions.model_copy(
        update=_updates(
            caption_m,
            ("font_name", "font_size", "alignment", "bold", "italic", "space_before", "space_after"),
        )
    )


    table_m = _collect_tables(document)
    tables = base.tables.model_copy(
        update=_updates(
            table_m,
            (
                "table_alignment", "font_name", "font_size", "italic", "header_bold",
                "cell_margin_top", "cell_margin_bottom", "cell_margin_left", "cell_margin_right",
            ),
        )
    )

    figure_align = _collect_figures(document)
    figures = FigureStyle(alignment=_pick(figure_align, base.figures.alignment))

    list_m = _collect_lists(document)
    lists = base.lists.model_copy(update=_updates(list_m, ("left_indent", "alignment")))

    # --- титульный лист, спецразделы и нумерация страниц (по структуре документа)
    detected_sections = _classify_document_sections(document, base)
    if detected_sections.title_page:
        title_page = _build_title_page_style(
            document, detected_sections.title_page, base.title_page
        )
    else:
        title_page = base.title_page
    special_sections = _build_special_sections_style(
        document, detected_sections.special, base.special_sections
    )
    # Профиль заголовка ПЕРВОГО уровня, извлечённый из документа, передаём в
    # названия спецразделов. «РЕФЕРАТ», «ЗАДАНИЕ» и «СОДЕРЖАНИЕ» должны
    # оформляться тем же кеглем и начертанием, что и структурные заголовки
    # («ВВЕДЕНИЕ», «ЗАКЛЮЧЕНИЕ»), иначе иерархия размеров терялась и часть
    # заголовков рендерилась кеглем основного текста.
    h1 = headings.get("h1")
    if isinstance(h1, HeadingStyle):
        special_sections.title_font_name = h1.font_name
        special_sections.title_font_size = h1.font_size
        special_sections.title_bold = bool(h1.bold)
        special_sections.title_italic = bool(h1.italic)
        special_sections.title_alignment = h1.alignment
        special_sections.title_first_line_indent = h1.first_line_indent

    page_numbering = _detect_page_numbering(document, base.page_numbering)

    report["detected"] = {
        "body": body_m,
        "headings": {f"h{k}": _collect_group(v, document) for k, v in heading_ps.items()},
        "captions": caption_m,
        "tables": table_m,
        "figures": {"alignment": figure_align.value if figure_align else None},
        "lists": list_m,
    }
    report["counts"] = {
        "body": len(body_ps),
        "headings": sum(len(v) for v in heading_ps.values()),
        "captions": len(caption_ps),
        "lists": len(list_ps),
        "tables": len(document.tables),
        "images": len(document.inline_shapes),
    }
    report["sections"] = {
        "title_page": len(detected_sections.title_page),
        "special": {k: len(v) for k, v in detected_sections.special.items()},
        "page_numbering": page_numbering.enabled,
    }

    return StylePreset(
        name=name,
        description=description,
        page=page,
        body=body,
        headings=headings,
        tables=tables,
        captions=captions,
        figures=figures,
        lists=lists,
        cleanup=base.cleanup,
        title_page=title_page,
        special_sections=special_sections,
        page_numbering=page_numbering,
    )


@dataclass
class AnalysisResult:
    """Результат анализа: пресет, отчёт и модель предпросмотра."""

    preset: StylePreset
    report: dict[str, Any] = field(default_factory=dict)
    preview: dict[str, Any] = field(default_factory=dict)


def analyze_document(
    source_path: str,
    *,
    name: str | None = None,
    description: str | None = None,
    include_images: bool = True,
) -> AnalysisResult:
    """Анализирует ``.docx`` и возвращает пресет, отчёт и предпросмотр."""
    source = Path(source_path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Документ не найден: {source}")
    try:
        document = docx.Document(str(source))
    except Exception as exc:  # noqa: BLE001 - python-docx бросает разные типы
        raise ValueError(f"Не удалось открыть документ {source}: {exc}") from exc

    preset_name = name or f"analyzed_{_safe_name(source.stem)}"
    preset_description = description or f"Пресет, извлечённый из «{source.name}»."
    report: dict[str, Any] = {"source": source.name}
    preset = _build_preset(document, preset_name, preset_description, report)
    preview = build_preview(document, include_images=include_images)
    preview["page"] = {
        "top": preset.page.top,
        "bottom": preset.page.bottom,
        "left": preset.page.left,
        "right": preset.page.right,
        "orientation": preset.page.orientation.value,
    }
    _log(f"word_automation: анализ '{source.name}' -> пресет '{preset.name}'")
    return AnalysisResult(preset=preset, report=report, preview=preview)


def _safe_name(text: str) -> str:
    """Безопасный короткий фрагмент имени файла."""
    cleaned = re.sub(r"[^\w\-]+", "_", str(text or "").strip(), flags=re.UNICODE)
    return (cleaned.strip("_") or "document")[:40]



def page_dict(document: DocumentObject) -> dict[str, Any]:
    """Поля и ориентация первой секции (для предпросмотра)."""
    base = default_gost_preset().page
    if document.sections:
        section = document.sections[0]
        return {
            "top": _pick(_mm_from_length(section.top_margin), base.top),
            "bottom": _pick(_mm_from_length(section.bottom_margin), base.bottom),
            "left": _pick(_mm_from_length(section.left_margin), base.left),
            "right": _pick(_mm_from_length(section.right_margin), base.right),
            "orientation": (
                Orientation.LANDSCAPE.value
                if section.orientation == WD_ORIENT.LANDSCAPE
                else Orientation.PORTRAIT.value
            ),
        }
    return {
        "top": base.top, "bottom": base.bottom, "left": base.left,
        "right": base.right, "orientation": base.orientation.value,
    }


def preview_document(source_path: str, *, include_images: bool = True) -> dict[str, Any]:
    """Возвращает модель предпросмотра документа по пути к ``.docx``."""
    source = Path(source_path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Документ не найден: {source}")
    document = docx.Document(str(source))
    preview = build_preview(document, include_images=include_images)
    preview["page"] = page_dict(document)
    return preview
