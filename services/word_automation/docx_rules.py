"""Точечное применение правил оформления к существующему DOCX.

Модуль — «руки» Document Studio: принимает список правил (scope + property +
value) и физически модифицирует ``.docx``. Используется и кнопкой «Применить»
в правой панели, и агентскими инструментами чата студии.

Поддерживаются все ключевые группы правил:

* ``page`` — поля страницы (top/bottom/left/right, мм);
* ``body``/``headings``/``h1``..``h9``/``captions``/``lists``/``all`` —
  интервалы, межстрочный интервал, отступ первой строки, выравнивание,
  шрифт/кегль/полужирный/курсив;
* ``tables`` — выравнивание таблицы на странице, выравнивание текста в ячейках,
  шрифт/кегль/начертание ячеек, отступы внутри ячеек;
* ``figures`` — выравнивание рисунков.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

import docx
from docx.document import Document as DocumentObject
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt
from docx.table import Table
from docx.text.paragraph import Paragraph

try:  # импорт внутри пакета
    from .docx_engine import (
        ALIGNMENT_MAP,
        LINE_RULE_MAP,
        _TBLPR_TAG_SEQ,
        _insert_in_order,
        _classify_document_sections,
        DocxFormatter,
        apply_paragraph_format,
        element_has_media,
        set_run_font,
    )
    from .docx_extractor import _classify_paragraph
    from .preset_schema import (
        Alignment,
        LineSpacingRule,
        PageNumberingStyle,
        PresetError,
        StylePreset,
        _log,
        default_gost_preset,
        resolve_preset,
    )
except ImportError:  # pragma: no cover - запуск модуля вне пакета
    from docx_engine import (  # type: ignore[no-redef]
        ALIGNMENT_MAP,
        LINE_RULE_MAP,
        _TBLPR_TAG_SEQ,
        _insert_in_order,
        _classify_document_sections,
        DocxFormatter,
        apply_paragraph_format,
        element_has_media,
        set_run_font,
    )
    from docx_extractor import _classify_paragraph  # type: ignore[no-redef]
    from preset_schema import (  # type: ignore[no-redef]
        Alignment,
        LineSpacingRule,
        PageNumberingStyle,
        PresetError,
        StylePreset,
        _log,
        default_gost_preset,
        resolve_preset,
    )


#: Твипсов на миллиметр (для ``w:tblCellMar``).
_TWIPS_PER_MM = 1440.0 / 25.4

#: Соответствие выравнивания таблицы на странице перечислению Word.
TABLE_ALIGNMENT_MAP: dict[Alignment, Any] = {
    Alignment.LEFT: WD_TABLE_ALIGNMENT.LEFT,
    Alignment.CENTER: WD_TABLE_ALIGNMENT.CENTER,
    Alignment.RIGHT: WD_TABLE_ALIGNMENT.RIGHT,
}

#: Описание поддерживаемых областей и свойств — для UI и агентских инструментов.
RULES_CATALOG: dict[str, tuple[str, ...]] = {
    "page": ("top", "bottom", "left", "right"),
    "body": (
        "alignment", "space_before", "space_after", "line_spacing",
        "line_spacing_rule", "first_line_indent", "font_name", "font_size",
        "bold", "italic", "color",
    ),
    "headings": (
        "alignment", "space_before", "space_after", "first_line_indent",
        "font_name", "font_size", "bold", "italic", "color",
    ),
    "captions": (
        "alignment", "space_before", "space_after",
        "font_name", "font_size", "bold", "italic", "color",
    ),
    "lists": ("alignment", "space_before", "space_after", "left_indent"),
    "tables": (
        "table_alignment", "alignment", "font_name", "font_size", "bold", "italic",
        "cell_margin_top", "cell_margin_bottom", "cell_margin_left", "cell_margin_right",
    ),
    "figures": ("alignment",),
    # Новые области: титульный лист, спецразделы, нумерация страниц.
    "title_page": (
        "alignment", "space_before", "space_after", "first_line_indent",
        "left_indent", "font_name", "font_size", "bold", "italic", "color",
    ),
    "special_sections": (
        "alignment", "space_before", "space_after", "first_line_indent",
        "line_spacing", "line_spacing_rule", "font_name", "font_size",
        "bold", "italic", "color",
    ),
    "page_numbering": (
        "enabled", "position", "alignment", "start_at", "show_from_page",
        "show_on_first_page", "show_on_task_page", "hide_on_special",
        "page_format", "font_name", "font_size",
    ),
}
#: Область «all» = все абзацы основного текста, заголовки и подписи.
RULES_CATALOG["all"] = tuple(
    sorted(set(RULES_CATALOG["body"]) | set(RULES_CATALOG["headings"]))
)


def _as_alignment(value: Any) -> Alignment | None:
    """Приводит значение к :class:`Alignment` (или ``None``)."""
    if value is None:
        return None
    if isinstance(value, Alignment):
        return value
    text = str(value).strip().lower()
    mapping = {
        "left": Alignment.LEFT, "center": Alignment.CENTER,
        "centre": Alignment.CENTER, "right": Alignment.RIGHT,
        "justify": Alignment.JUSTIFY, "both": Alignment.JUSTIFY,
        "по левому": Alignment.LEFT, "по центру": Alignment.CENTER,
        "по правому": Alignment.RIGHT, "по ширине": Alignment.JUSTIFY,
    }
    if text in mapping:
        return mapping[text]
    try:
        return Alignment(text)
    except ValueError:
        return None


def _as_rule(value: Any) -> LineSpacingRule | None:
    """Приводит значение к :class:`LineSpacingRule`."""
    if value is None:
        return None
    if isinstance(value, LineSpacingRule):
        return value
    try:
        return LineSpacingRule(str(value).strip().lower())
    except ValueError:
        return None


def _scopes_of(paragraph: Paragraph) -> set[str]:
    """Набор областей, к которым относится абзац."""
    kind, level = _classify_paragraph(paragraph)
    tags = {"all"}
    if kind == "heading":
        tags.add("headings")
        tags.add(f"h{max(1, min(level, 9))}")
    elif kind == "caption":
        tags.add("captions")
    elif kind == "list":
        tags.add("lists")
    else:
        tags.add("body")
    return tags


# --------------------------------------------------------------------------------------
# Применение правил к абзацам
# --------------------------------------------------------------------------------------


def _iter_top_paragraphs(document: DocumentObject) -> Iterable[Paragraph]:
    """Абзацы верхнего уровня (без ячеек таблиц) в порядке следования."""
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)


def _apply_paragraph_property(paragraph: Paragraph, prop: str, value: Any) -> bool:
    """Применяет одно свойство к абзацу (и его ранам). Возвращает True при успехе."""
    pf = paragraph.paragraph_format
    if prop == "alignment":
        alignment = _as_alignment(value)
        if alignment is None:
            return False
        pf.alignment = ALIGNMENT_MAP[alignment]
        return True
    if prop == "space_before":
        pf.space_before = Pt(float(value))
        return True
    if prop == "space_after":
        pf.space_after = Pt(float(value))
        return True
    if prop == "line_spacing":
        pf.line_spacing = float(value)
        return True
    if prop == "line_spacing_rule":
        rule = _as_rule(value)
        if rule is None:
            return False
        pf.line_spacing_rule = LINE_RULE_MAP[rule]
        return True
    if prop == "first_line_indent":
        pf.first_line_indent = Mm(float(value))
        return True
    if prop == "left_indent":
        pf.left_indent = Mm(float(value))
        return True
    if prop in ("font_name", "font_size", "bold", "italic", "color"):
        _apply_font_property(paragraph, prop, value)
        return True
    return False


def _apply_font_property(paragraph: Paragraph, prop: str, value: Any) -> None:
    """Меняет шрифт/кегль/начертание во всех ранах абзаца."""
    kwargs: dict[str, Any] = {}
    if prop == "font_name":
        kwargs["font_name"] = str(value)
    elif prop == "font_size":
        kwargs["size_pt"] = float(value)
    elif prop == "bold":
        kwargs["bold"] = bool(value)
    elif prop == "italic":
        kwargs["italic"] = bool(value)
    elif prop == "color":
        kwargs["color"] = str(value)
    for run in paragraph.runs:
        if element_has_media(run._r):
            continue
        set_run_font(run, **kwargs)


def _apply_paragraph_rule(
    document: DocumentObject, scope: str, prop: str, value: Any, stats: dict[str, Any]
) -> None:
    """Применяет правило к абзацам выбранной области."""
    if scope not in RULES_CATALOG:
        raise PresetError(f"Неизвестная область правила: '{scope}'")
    if prop not in RULES_CATALOG.get(scope, ()):
        raise PresetError(
            f"Свойство '{prop}' не поддерживается для области '{scope}'"
        )
    touched = 0
    for paragraph in _iter_top_paragraphs(document):
        if scope not in _scopes_of(paragraph):
            continue
        if _apply_paragraph_property(paragraph, prop, value):
            touched += 1
    stats["paragraphs_touched"] = stats.get("paragraphs_touched", 0) + touched



# --------------------------------------------------------------------------------------
# Применение правил к таблицам
# --------------------------------------------------------------------------------------


def _set_table_cell_margins(table: Table, margins: dict[str, float]) -> None:
    """Записывает ``w:tblCellMar`` (отступы внутри ячеек) в правильную позицию."""
    tblPr = table._tbl.tblPr
    existing = tblPr.find(qn("w:tblCellMar"))
    if existing is not None:
        tblPr.remove(existing)
    mar = OxmlElement("w:tblCellMar")
    for side in ("top", "bottom", "left", "right"):
        if side not in margins:
            continue
        element = OxmlElement(f"w:{side}")
        element.set(qn("w:w"), str(int(round(float(margins[side]) * _TWIPS_PER_MM))))
        element.set(qn("w:type"), "dxa")
        mar.append(element)
    if len(mar):
        _insert_in_order(tblPr, mar, _TBLPR_TAG_SEQ)


def _apply_table_property(
    document: DocumentObject, prop: str, value: Any, stats: dict[str, Any]
) -> None:
    """Применяет одно свойство ко всем таблицам документа."""
    if prop not in RULES_CATALOG["tables"]:
        raise PresetError(f"Свойство '{prop}' не поддерживается для таблиц")
    touched = 0
    if prop == "table_alignment":
        alignment = _as_alignment(value)
        if alignment is None:
            raise PresetError(f"Некорректное выравнивание таблицы: {value!r}")
        for table in document.tables:
            table.alignment = TABLE_ALIGNMENT_MAP[alignment]
            touched += 1
    elif prop.startswith("cell_margin_"):
        side = prop.replace("cell_margin_", "")
        for table in document.tables:
            _set_table_cell_margins(table, {side: float(value)})
            touched += 1
    else:
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        _apply_paragraph_property(paragraph, prop, value)
            touched += 1
    stats["tables_touched"] = stats.get("tables_touched", 0) + touched


# --------------------------------------------------------------------------------------
# Применение правил к рисункам и странице
# --------------------------------------------------------------------------------------


def _apply_figure_property(
    document: DocumentObject, prop: str, value: Any, stats: dict[str, Any]
) -> None:
    """Выравнивает абзацы, содержащие рисунки."""
    if prop != "alignment":
        raise PresetError(f"Свойство '{prop}' не поддерживается для рисунков")
    alignment = _as_alignment(value)
    if alignment is None:
        raise PresetError(f"Некорректное выравнивание рисунка: {value!r}")
    touched = 0
    for paragraph in _iter_top_paragraphs(document):
        if paragraph._p.find(".//" + qn("w:drawing")) is None and paragraph._p.find(
            ".//" + qn("w:pict")
        ) is None:
            continue
        paragraph.paragraph_format.alignment = ALIGNMENT_MAP[alignment]
        touched += 1
    stats["figures_touched"] = stats.get("figures_touched", 0) + touched


def _apply_page_property(
    document: DocumentObject, prop: str, value: Any, stats: dict[str, Any]
) -> None:
    """Меняет поля страницы во всех секциях."""
    if prop not in RULES_CATALOG["page"]:
        raise PresetError(f"Свойство '{prop}' не поддерживается для страницы")
    for section in document.sections:
        setattr(section, f"{prop}_margin", Mm(float(value)))
    stats["sections_touched"] = stats.get("sections_touched", 0) + len(document.sections)


#: Свойства, которые применяются к ранам (шрифт), а не к абзацу.
_FONT_PROPERTIES = ("font_name", "font_size", "bold", "italic", "color")


def _apply_section_paragraphs_property(
    document: DocumentObject, prop: str, value: Any, stats: dict[str, Any], *, only: str
) -> None:
    """Применяет свойство к абзацам титульного листа или спецразделов."""
    if prop not in RULES_CATALOG[only]:
        raise PresetError(f"Свойство '{prop}' не поддерживается для области '{only}'")
    detected = _classify_document_sections(document, default_gost_preset())
    if only == "title_page":
        paragraphs = list(detected.title_page)
    else:
        paragraphs = [p for group in detected.special.values() for p in group]
    touched = 0
    for paragraph in paragraphs:
        if prop in _FONT_PROPERTIES:
            _apply_font_property(paragraph, prop, value)
        elif not _apply_paragraph_property(paragraph, prop, value):
            continue
        touched += 1
    key = "title_page_touched" if only == "title_page" else "special_sections_touched"
    stats[key] = stats.get(key, 0) + touched


def _apply_page_numbering_property(
    document: DocumentObject, prop: str, value: Any, stats: dict[str, Any]
) -> None:
    """Меняет настройку нумерации страниц и заново настраивает колонтитулы."""
    if prop not in RULES_CATALOG["page_numbering"]:
        raise PresetError(f"Свойство '{prop}' не поддерживается для нумерации страниц")
    data = default_gost_preset().page_numbering.model_dump(mode="json")
    data[prop] = value
    if prop != "enabled":
        data["enabled"] = True
    try:
        cfg = PageNumberingStyle.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - внятное сообщение об ошибке значения
        raise PresetError(
            f"Некорректное значение нумерации '{prop}': {value!r} ({exc})"
        ) from exc
    preset = default_gost_preset().model_copy(update={"page_numbering": cfg})
    DocxFormatter()._apply_page_numbering(document, preset)
    stats["page_numbering_touched"] = stats.get("page_numbering_touched", 0) + len(
        document.sections
    )



# --------------------------------------------------------------------------------------
# Публичное API
# --------------------------------------------------------------------------------------


def _normalize_rules(rules: Any) -> list[dict[str, Any]]:
    """Приводит список правил к каноническому виду ``scope/property/value``."""
    out: list[dict[str, Any]] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        scope = str(rule.get("scope") or rule.get("target") or "").strip().lower()
        prop = str(rule.get("property") or rule.get("prop") or "").strip().lower()
        if not scope or not prop:
            continue
        out.append({"scope": scope, "property": prop, "value": rule.get("value")})
    return out


def _dispatch_rule(
    document: DocumentObject, rule: dict[str, Any], stats: dict[str, Any]
) -> None:
    """Направляет одно правило обработчику нужной области."""
    scope = rule["scope"]
    prop = rule["property"]
    value = rule["value"]
    if scope == "page":
        _apply_page_property(document, prop, value, stats)
    elif scope == "tables":
        _apply_table_property(document, prop, value, stats)
    elif scope == "figures":
        _apply_figure_property(document, prop, value, stats)
    elif scope == "title_page":
        _apply_section_paragraphs_property(document, prop, value, stats, only="title_page")
    elif scope == "special_sections":
        _apply_section_paragraphs_property(
            document, prop, value, stats, only="special_sections"
        )
    elif scope == "page_numbering":
        _apply_page_numbering_property(document, prop, value, stats)
    else:
        _apply_paragraph_rule(document, scope, prop, value, stats)


def resolve_output_path(source: Path, output_path: str = "") -> Path:
    """Определяет путь результата (по умолчанию — рядом с исходником)."""
    if output_path:
        return Path(output_path)
    return source.with_name(f"{source.stem}_styled{source.suffix or '.docx'}")


def apply_rules(
    source_path: str,
    rules: Any,
    *,
    output_path: str = "",
) -> dict[str, Any]:
    """Применяет список правил к документу и сохраняет результат.

    ``rules`` — список словарей ``{"scope": ..., "property": ..., "value": ...}``.
    Возвращает ``{"path", "filename", "stats"}``.
    """
    source = Path(source_path)
    if not source.exists() or not source.is_file():
        raise PresetError(f"Документ не найден: {source}")
    try:
        document = docx.Document(str(source))
    except Exception as exc:  # noqa: BLE001 - python-docx бросает разные типы
        raise PresetError(f"Не удалось открыть документ {source}: {exc}") from exc

    normalized = _normalize_rules(rules)
    if not normalized:
        raise PresetError("Не передано ни одного корректного правила")

    stats: dict[str, Any] = {"rules_applied": 0}
    for rule in normalized:
        _dispatch_rule(document, rule, stats)
        stats["rules_applied"] += 1

    target = resolve_output_path(source, output_path)
    try:
        document.save(str(target))
    except (OSError, ValueError) as exc:
        raise PresetError(f"Не удалось сохранить документ {target}: {exc}") from exc
    _log(f"word_automation: применено {stats['rules_applied']} правил -> {target.name}")
    return {"path": str(target), "filename": target.name, "stats": stats}


def reset_spacing(source_path: str, scope: str = "all", *, output_path: str = "") -> dict[str, Any]:
    """Сбрасывает интервалы до/после и межстрочный интервал для области."""
    return apply_rules(
        source_path,
        [
            {"scope": scope, "property": "space_before", "value": 0.0},
            {"scope": scope, "property": "space_after", "value": 0.0},
            {"scope": scope, "property": "line_spacing", "value": 1.0},
            {"scope": scope, "property": "line_spacing_rule", "value": "single"},
        ],
        output_path=output_path,
    )


def apply_preset(
    source_path: str,
    preset: "StylePreset | str | dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Нормализует документ по пресету и донакатывает поля таблиц/рисунков."""
    resolved = resolve_preset(preset)
    normalized = DocxFormatter().normalize_document(str(source_path), preset=resolved)
    stats: dict[str, Any] = {"preset": resolved.name, "path": normalized}

    document = docx.Document(normalized)
    tables = resolved.tables
    if tables.table_alignment is not None:
        _apply_table_property(document, "table_alignment", tables.table_alignment, stats)
    for side in ("top", "bottom", "left", "right"):
        _apply_table_property(
            document, f"cell_margin_{side}", getattr(tables, f"cell_margin_{side}"), stats
        )
    _apply_figure_property(document, "alignment", resolved.figures.alignment, stats)
    try:
        document.save(normalized)
    except (OSError, ValueError) as exc:
        raise PresetError(f"Не удалось сохранить документ {normalized}: {exc}") from exc

    return {"path": normalized, "filename": os.path.basename(normalized), "stats": stats}
