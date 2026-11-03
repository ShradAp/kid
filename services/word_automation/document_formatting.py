"""OOXML font, paragraph and table formatting shared by document workflows."""

from __future__ import annotations

from typing import Any, Sequence
from docx.document import Document as DocumentObject
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

try:
    from .preset_schema import Alignment, CellVerticalAlignment, LineSpacingRule, POINT_BASED_RULES, TableStyle
except ImportError:
    from preset_schema import Alignment, CellVerticalAlignment, LineSpacingRule, POINT_BASED_RULES, TableStyle


ALIGNMENT_MAP: dict[Alignment, Any] = {
    Alignment.LEFT: WD_ALIGN_PARAGRAPH.LEFT,
    Alignment.CENTER: WD_ALIGN_PARAGRAPH.CENTER,
    Alignment.RIGHT: WD_ALIGN_PARAGRAPH.RIGHT,
    Alignment.JUSTIFY: WD_ALIGN_PARAGRAPH.JUSTIFY,
}


LINE_RULE_MAP: dict[LineSpacingRule, Any] = {
    LineSpacingRule.SINGLE: WD_LINE_SPACING.SINGLE,
    LineSpacingRule.ONE_POINT_FIVE: WD_LINE_SPACING.ONE_POINT_FIVE,
    LineSpacingRule.DOUBLE: WD_LINE_SPACING.DOUBLE,
    LineSpacingRule.MULTIPLE: WD_LINE_SPACING.MULTIPLE,
    LineSpacingRule.EXACTLY: WD_LINE_SPACING.EXACTLY,
    LineSpacingRule.AT_LEAST: WD_LINE_SPACING.AT_LEAST,
}


TABLE_BORDER_EDGES = ("top", "left", "bottom", "right", "insideH", "insideV")


TABLE_ALIGNMENT_MAP: dict[Alignment, Any] = {
    Alignment.LEFT: WD_TABLE_ALIGNMENT.LEFT,
    Alignment.CENTER: WD_TABLE_ALIGNMENT.CENTER,
    Alignment.RIGHT: WD_TABLE_ALIGNMENT.RIGHT,
    Alignment.JUSTIFY: WD_TABLE_ALIGNMENT.CENTER,
}


CELL_VALIGN_MAP: dict[CellVerticalAlignment, Any] = {
    CellVerticalAlignment.TOP: WD_ALIGN_VERTICAL.TOP,
    CellVerticalAlignment.CENTER: WD_ALIGN_VERTICAL.CENTER,
    CellVerticalAlignment.BOTTOM: WD_ALIGN_VERTICAL.BOTTOM,
}


_TBLPR_TAG_SEQ = (
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual",
    "w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
    "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout",
    "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription",
    "w:tblPrChange",
)

#: Порядок детей ``w:trPr`` по схеме OOXML (для cantSplit / tblHeader).
_TRPR_TAG_SEQ = (
    "w:cnfStyle", "w:divId", "w:gridBefore", "w:gridAfter", "w:wBefore",
    "w:wAfter", "w:cantSplit", "w:trHeight", "w:tblHeader", "w:tblCellSpacing",
    "w:jc", "w:hidden", "w:ins", "w:del", "w:trPrChange",
)

#: Тематические атрибуты шрифта, перебивающие явно заданное имя шрифта.
_RFONT_THEME_ATTRS = ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme")
#: Тематические атрибуты цвета, перебивающие явно заданный RGB.
_COLOR_THEME_ATTRS = ("w:themeColor", "w:themeTint", "w:themeShade")
#: Признаки медиа/полей внутри рана — такие раны нельзя пересобирать по тексту.
_RUN_MEDIA_TAGS = (
    "w:drawing", "w:pict", "w:object", "w:fldChar", "w:instrText",
    "w:footnoteReference", "w:endnoteReference", "w:commentReference",
)


def _insert_in_order(parent: Any, element: Any, tag_seq: Sequence[str]) -> None:
    """Вставляет элемент в ``parent`` согласно порядку тегов схемы OOXML."""
    tag_name = element.tag
    try:
        own_name = next(name for name in tag_seq if qn(name) == tag_name)
    except StopIteration:
        parent.append(element)
        return
    index = list(tag_seq).index(own_name)
    successors = {qn(name) for name in tag_seq[index + 1:]}
    for child in parent:
        if child.tag in successors:
            child.addprevious(element)
            return
    parent.append(element)


def _set_font_on(
    font: Any,
    rPr_owner: Any,
    *,
    font_name: str | None = None,
    size_pt: float | None = None,
    color: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
) -> None:
    """Универсальная установка шрифта для рана или стиля.

    ``font`` — прокси ``run.font``/``style.font``; ``rPr_owner`` — элемент,
    имеющий ``get_or_add_rPr()`` (``w:r`` у рана, ``w:style`` у стиля).

    Тематические атрибуты (``w:asciiTheme``, ``w:themeColor``) перебивают явные
    значения в стилях ``Heading N`` — без их удаления пресет не применяется.
    """
    if font_name:
        font.name = font_name
        rPr = rPr_owner.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            rFonts.set(qn(attr), font_name)
        for attr in _RFONT_THEME_ATTRS:
            if rFonts.get(qn(attr)) is not None:
                del rFonts.attrib[qn(attr)]

    if size_pt is not None:
        font.size = Pt(size_pt)
        rPr = rPr_owner.get_or_add_rPr()
        sz_cs = rPr.find(qn("w:szCs"))
        if sz_cs is None:
            sz_cs = OxmlElement("w:szCs")
            rPr.append(sz_cs)
        sz_cs.set(qn("w:val"), str(int(round(size_pt * 2))))

    if color:
        rPr = rPr_owner.get_or_add_rPr()
        color_el = rPr.find(qn("w:color"))
        if color == "auto":
            # «auto» = наследовать цвет из стиля: снимаем явное значение.
            if color_el is not None:
                rPr.remove(color_el)
        else:
            font.color.rgb = RGBColor.from_string(color)
            color_el = rPr.find(qn("w:color"))
            if color_el is not None:
                for attr in _COLOR_THEME_ATTRS:
                    if color_el.get(qn(attr)) is not None:
                        del color_el.attrib[qn(attr)]

    if bold is not None:
        font.bold = bold
    if italic is not None:
        font.italic = italic
    if underline is not None:
        font.underline = underline


def set_run_font(
    run: Run,
    *,
    font_name: str | None = None,
    size_pt: float | None = None,
    color: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
) -> None:
    """Задаёт шрифт/кегль/цвет/начертание рана (см. :func:`_set_font_on`)."""
    _set_font_on(
        run.font,
        run._r,
        font_name=font_name,
        size_pt=size_pt,
        color=color,
        bold=bold,
        italic=italic,
        underline=underline,
    )


def set_style_font(
    style: Any,
    *,
    font_name: str | None = None,
    size_pt: float | None = None,
    color: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
) -> None:
    """Задаёт шрифт/кегль/цвет/начертание стиля ``styles.xml``."""
    _set_font_on(
        style.font,
        style.element,
        font_name=font_name,
        size_pt=size_pt,
        color=color,
        bold=bold,
        italic=italic,
    )


def _style_or_none(document: DocumentObject, name: str) -> Any:
    """Возвращает стиль по имени либо ``None``, если его нет в шаблоне."""
    try:
        return document.styles[name]
    except (KeyError, ValueError):
        return None



def apply_line_spacing(pf: Any, line_spacing: float, rule: LineSpacingRule) -> None:
    """Применяет межстрочный интервал с учётом правила.

    Для ``exactly``/``at_least`` значение трактуется как пункты (``Length``),
    иначе — как кратность высоты строки (``float``). Порядок важен: сначала
    задаётся значение, затем правило (иначе python-docx перетрёт значение).
    """
    if rule in POINT_BASED_RULES:
        pf.line_spacing = Pt(line_spacing)
        pf.line_spacing_rule = LINE_RULE_MAP[rule]
    else:
        pf.line_spacing = float(line_spacing)



def apply_paragraph_format(
    pf: Any,
    *,
    alignment: Alignment | None = None,
    space_before: float | None = None,
    space_after: float | None = None,
    line_spacing: float | None = None,
    line_spacing_rule: LineSpacingRule | None = None,
    first_line_indent_mm: float | None = None,
    left_indent_mm: float | None = None,
    right_indent_mm: float | None = None,
    keep_with_next: bool | None = None,
    widow_control: bool | None = None,
    page_break_before: bool | None = None,
) -> None:
    """Накатывает параметры абзаца; ``None`` означает «не трогать»."""
    if alignment is not None:
        pf.alignment = ALIGNMENT_MAP[alignment]
    if space_before is not None:
        pf.space_before = Pt(space_before)
    if space_after is not None:
        pf.space_after = Pt(space_after)
    if line_spacing is not None and line_spacing_rule is not None:
        apply_line_spacing(pf, line_spacing, line_spacing_rule)
    if first_line_indent_mm is not None:
        pf.first_line_indent = Mm(first_line_indent_mm)
    if left_indent_mm is not None:
        pf.left_indent = Mm(left_indent_mm)
    if right_indent_mm is not None:
        pf.right_indent = Mm(right_indent_mm)
    if keep_with_next is not None:
        pf.keep_with_next = keep_with_next
    if widow_control is not None:
        pf.widow_control = widow_control
    if page_break_before is not None:
        pf.page_break_before = page_break_before


def set_table_borders(table: Table, cfg: TableStyle) -> None:
    """Записывает ``w:tblBorders`` (видимые либо ``none``) в правильную позицию."""
    tblPr = table._tbl.tblPr
    existing = tblPr.find(qn("w:tblBorders"))
    if existing is not None:
        tblPr.remove(existing)
    borders = OxmlElement("w:tblBorders")
    value = "single" if cfg.borders else "none"
    size = str(cfg.border_sz_eighths)
    for edge in TABLE_BORDER_EDGES:
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), value)
        element.set(qn("w:sz"), size)
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), cfg.border_color)
        borders.append(element)
    _insert_in_order(tblPr, borders, _TBLPR_TAG_SEQ)


def _flag_element(tag: str) -> Any:
    """Создаёт флаг-элемент ``w:cantSplit``/``w:tblHeader`` со значением true."""
    element = OxmlElement(tag)
    element.set(qn("w:val"), "true")
    return element


def set_row_properties(
    row: Any, *, cant_split: bool | None = None, tbl_header: bool | None = None
) -> None:
    """Выставляет/снимает ``w:cantSplit`` и ``w:tblHeader`` для строки таблицы."""
    if cant_split is None and tbl_header is None:
        return
    trPr = row._tr.get_or_add_trPr()
    for tag, enabled in (("w:cantSplit", cant_split), ("w:tblHeader", tbl_header)):
        if enabled is None:
            continue
        existing = trPr.find(qn(tag))
        if existing is not None:
            trPr.remove(existing)
        if enabled:
            _insert_in_order(trPr, _flag_element(tag), _TRPR_TAG_SEQ)


def element_has_media(element: Any) -> bool:
    """True, если внутри есть картинки/объекты/поля — их нельзя пересобирать."""
    for tag in _RUN_MEDIA_TAGS:
        qualified = qn(tag)
        if element.find(qualified) is not None:
            return True
        if element.findall(".//" + qualified):
            return True
    return False


def remove_paragraph(paragraph: Paragraph) -> None:
    """Удаляет параграф из тела документа."""
    element = paragraph._p
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def ensure_document_not_empty(document: DocumentObject) -> None:
    """Гарантирует наличие хотя бы одного параграфа (требование OOXML/Word)."""
    body = document.element.body
    has_block = any(
        child.tag in (qn("w:p"), qn("w:tbl")) for child in body.iterchildren()
    )
    if not has_block:
        document.add_paragraph("")

