"""Recognition of document sections, structural headings and list markers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

try:
    from .preset_schema import SPECIAL_SECTION_KINDS, StylePreset
except ImportError:
    from preset_schema import SPECIAL_SECTION_KINDS, StylePreset


_LIST_MARKER_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[\.\)]|[а-яa-z][\.\)]|[–—•·\-\*o])\s+\S", re.IGNORECASE
)


_LEADING_MARKER_RE = re.compile(
    r"^\s*(?P<marker>\d{1,3}(?:\.\d+)*[.\)]|[а-яёa-zА-ЯЁA-Z][.\)]|[–—•·\*\-])"
    r"(?P<sep>\s+)?(?P<rest>\S.*)$",
    re.UNICODE,
)


_SECTION_NUMBER_RE = re.compile(
    r"^(?P<number>\d{1,3}(?:\.\d{1,3})*)(?P<delim>\.|\s)\s*(?P<title>\S.*)$",
    re.UNICODE,
)


_MAX_HEADING_WORDS = 12


_DEFAULT_STRUCTURAL_TITLES = (
    "реферат", "содержание", "оглавление", "введение", "заключение",
    "список", "перечень", "приложение", "обозначен", "сокращен",
    "определения", "термины", "аннотация", "выводы",
)


_UNDERLINE_GAP_RE = re.compile(r"_{3,}")


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


_MIN_KEYWORD_LEN = 3


def _paragraph_breaks_page(paragraph: Paragraph) -> bool:
    """True, если абзац начинает новую страницу или содержит разрыв страницы."""
    element = paragraph._p
    pPr = element.find(qn("w:pPr"))
    if pPr is not None and pPr.find(qn("w:pageBreakBefore")) is not None:
        return True
    for br in element.findall(".//" + qn("w:br")):
        if br.get(qn("w:type")) == "page":
            return True
    return element.find(".//" + qn("w:lastRenderedPageBreak")) is not None


def _normalize_match_text(text: str) -> str:
    """Приводит строку к виду для сравнения с ключевыми словами."""
    cleaned = re.sub(r"[^\w\s]+", " ", text or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _matches_any(text: str, keywords: Sequence[str]) -> bool:
    """True, если начало текста содержит одно из ключевых слов."""
    normalized = _normalize_match_text(text)
    if not normalized:
        return False
    head = normalized[:60]
    return any(
        str(kw).strip().lower() in head
        for kw in keywords
        if str(kw).strip() and len(str(kw).strip()) >= _MIN_KEYWORD_LEN
    )


def _paragraph_has_underline(paragraph: Paragraph) -> bool:
    """True, если в абзаце есть подчёркнутый текст или линия ``____``."""
    if _UNDERLINE_GAP_RE.search(paragraph.text or ""):
        return True
    for run in paragraph.runs:
        if run.font.underline:
            return True
    return False


def _is_year_line(text: str) -> bool:
    """Строка похожа на подвал титульника: содержит год."""
    return bool(_YEAR_RE.search(text or ""))


@dataclass
class DocumentSections:
    """Разбиение абзацев документа на титульный лист и специальные разделы."""

    title_page: list[Paragraph] = field(default_factory=list)
    special: dict[str, list[Paragraph]] = field(default_factory=dict)
    #: Структурные заголовки (ВВЕДЕНИЕ, ЗАКЛЮЧЕНИЕ, СПИСОК ...) — новая страница.
    structural: list[Paragraph] = field(default_factory=list)
    #: Нумерованные подзаголовки (1.1, 1.1.1 ...) — профиль hN БЕЗ новой страницы.
    subheadings: list[tuple[int, Paragraph]] = field(default_factory=list)
    #: индекс абзаца в теле (среди ``w:p``) -> тип секции.
    kind_by_index: dict[int, str] = field(default_factory=dict)

    def kind_for(self, index: int) -> str | None:
        """Тип секции по индексу абзаца (``None`` — обычный текст)."""
        return self.kind_by_index.get(index)

    @property
    def has_special(self) -> bool:
        """Найдены ли специальные разделы."""
        return bool(self.special)


_TITLE_WINDOW = 45


_MAX_FOOTER_LEN = 40


def _has_explicit_page_break(paragraph: Paragraph) -> bool:
    """True, если в абзаце есть ЯВНЫЙ разрыв страницы.

    В отличие от :func:`_paragraph_breaks_page` не учитывает
    ``w:lastRenderedPageBreak`` — это служебная метка Word о том, где страница
    фактически закончилась при последнем рендере. Опора на неё принимала за
    границу титула любой абзац, случайно оказавшийся вверху страницы.
    """
    element = paragraph._p
    pPr = element.find(qn("w:pPr"))
    if pPr is not None:
        pbb = pPr.find(qn("w:pageBreakBefore"))
        # ``w:val="0"/"false"`` — ВЫКЛЮЧЕННЫЙ разрыв (Word оставляет такой тег после
        # сброса свойства), настоящим разрывом он не является.
        if pbb is not None and pbb.get(qn("w:val")) not in ("0", "false", "none"):
            return True
    for br in element.findall(".//" + qn("w:br")):
        if br.get(qn("w:type")) == "page":
            return True
    return False


def _classify_document_sections(
    document: DocumentObject, preset: StylePreset
) -> DocumentSections:
    """Определяет границы титульного листа и спецразделов по структуре документа.

    Титульный лист — абзацы до первого спецраздела или до первого разрыва
    страницы. Спецразделы (реферат/задание/содержание) начинаются со своего
    заголовка-ключа и тянутся до начала следующего спецраздела.
    """
    result = DocumentSections()
    special_cfg = preset.special_sections
    title_cfg = preset.title_page

    paragraphs = [
        Paragraph(child, document)
        for child in document.element.body.iterchildren()
        if child.tag == qn("w:p")
    ]
    if not paragraphs:
        return result

    # 1. Первый спецраздел — только по СТРОГОМУ совпадению названия раздела.
    #    Прежний поиск подстроки принимал строку подписи «Задание приняла к
    #    исполнению … Петрова П.П.» за начало раздела «ЗАДАНИЕ», из-за чего
    #    титульный лист обрезался на подписях, а «город + год» уезжал на лист 2.
    first_special: int | None = None
    if special_cfg.enabled:
        for index, paragraph in enumerate(paragraphs):
            for kind in SPECIAL_SECTION_KINDS:
                if kind not in special_cfg.apply_to:
                    continue
                if _is_section_title_line(paragraph.text, special_cfg.titles_for(kind)):
                    first_special = index
                    break
            if first_special is not None:
                break

    # 2. Явный разрыв страницы и блок подвала («город + год») внутри титула.
    scan_end = first_special if first_special is not None else len(paragraphs)
    page_break_at: int | None = None
    for index in range(scan_end):
        if _has_explicit_page_break(paragraphs[index]):
            page_break_at = index
            break

    search_end = (
        first_special
        if first_special is not None
        else min(len(paragraphs), _TITLE_WINDOW)
    )
    footer_at: int | None = None
    for index in range(search_end):
        text = paragraphs[index].text.strip()
        if text and len(text) <= _MAX_FOOTER_LEN and _is_year_line(text):
            footer_at = index

    # Граница титула — ближайшая из достоверных. Разрыв, стоящий ПЕРЕД подвалом,
    # границей не считается: титул продлевается до конца блока «город + год», а
    # сам разрыв снимается при вёрстке (разрыв должен идти ПОСЛЕ года).
    candidates: list[int] = []
    if first_special is not None:
        candidates.append(first_special)
    if page_break_at is not None and (footer_at is None or page_break_at > footer_at):
        candidates.append(page_break_at + 1)
    if footer_at is not None:
        candidates.append(footer_at + 1)
    cut = min(candidates) if candidates else 0

    # 3. Титульный лист применяем только при явных признаках: разрыв страницы,
    #    следующий спецраздел, подвал с годом или подписи на первой странице.
    title_evidence = page_break_at is not None or first_special is not None
    if not title_evidence and footer_at is not None:
        title_evidence = True
    if not title_evidence and title_cfg.enabled:
        for paragraph in paragraphs[:cut]:
            text = paragraph.text.strip()
            if not text:
                continue
            if _is_year_line(text) or _paragraph_has_underline(paragraph):
                title_evidence = True
                break
            if _matches_any(text, title_cfg.signature_keywords):
                title_evidence = True
                break

    if title_cfg.enabled and title_evidence and cut > 0:
        for index in range(cut):
            result.title_page.append(paragraphs[index])
            result.kind_by_index[index] = "title_page"

    # 4. Спецразделы и заголовки разделов. Структурный заголовок (ВВЕДЕНИЕ,
    #    ЗАКЛЮЧЕНИЕ, СПИСКИ, ПРИЛОЖЕНИЯ) закрывает спецраздел и начинается с
    #    новой страницы. Нумерованные заголовки распознаются только ВНЕ
    #    спецразделов, чтобы пункт «1. Содержание работы:» бланка задания не
    #    превращался в заголовок раздела.
    current: str | None = None
    #: Шапка первого бланка (ведомство/вуз ПЕРЕД заголовком «ЗАДАНИЕ») лежит между
    #: концом титульного листа и первым спецразделом. Она относится к этому разделу,
    #: иначе остаётся «сиротой»: получает стиль основного текста и теряет разрыв
    #: страницы, а заголовок «ЗАДАНИЕ» уезжает на следующую страницу.
    leading: list[tuple[int, Paragraph]] = []
    first_special_seen = False
    for index in range(cut, len(paragraphs)):
        paragraph = paragraphs[index]
        text = paragraph.text.strip()
        matched: str | None = None
        if special_cfg.enabled:
            for kind in SPECIAL_SECTION_KINDS:
                if kind not in special_cfg.apply_to:
                    continue
                if _is_section_title_line(text, special_cfg.titles_for(kind)):
                    matched = kind
                    break
        if matched is not None:
            current = matched
            target = result.special.setdefault(current, [])
            if not first_special_seen:
                first_special_seen = True
                # Шапка бланка задания (ведомство/вуз) идёт ПЕРЕД заголовком
                # «ЗАДАНИЕ»; относим её к этому разделу, а не к «сиротам».
                # У реферата/содержания шапки нет — для них leading не трогаем.
                if matched == "task":
                    for _index, _paragraph in leading:
                        target.append(_paragraph)
                        result.kind_by_index[_index] = current
            leading = []
            target.append(paragraph)
            result.kind_by_index[index] = current
            continue
        if text:
            level = _section_heading_level(
                text, preset, allow_numbered=current is None
            )
            if level == 1:
                current = None
                leading = []
                result.structural.append(paragraph)
                result.kind_by_index[index] = "structure"
                continue
            if level is not None and current is None:
                leading = []
                result.subheadings.append((level, paragraph))
                result.kind_by_index[index] = f"heading{level}"
                continue
        if current is None:
            # Возможная шапка первого бланка (до встречи его заголовка). Пустые
            # абзацы (в т.ч. разрывы страницы) не трогаем — их разберёт чистка
            # распорок в _sanitize_body.
            if text:
                leading.append((index, paragraph))
            continue
        # Пустой абзац-разрыв страницы закрывает текущий спецраздел.
        starts_new_page = _paragraph_breaks_page(paragraph) and not text
        if starts_new_page and result.special.get(current):
            current = None
            continue
        result.special.setdefault(current, []).append(paragraph)
        result.kind_by_index[index] = current
    return result


def _footer_start_index(
    paragraphs: list[Paragraph],
    signature_keywords: Sequence[str] = (),
) -> int | None:
    """Индекс первого абзаца подвала титульного листа («Город, Год»).

    Подвал — это только блок «город + год» в самом конце титула. Раньше функция
    жадно включала в подвал ещё и предыдущий короткий абзац, из-за чего строка
    подписи («Петрова П.П. ______________») получала вертикальную распорку
    подвала (240 pt) ВМЕСТЕ с «Иваново, 2026 г.». Две распорки по 240 pt
    (~169 мм) не помещались в ~257 мм текста A4, титульный лист перетекал на
    вторую страницу, а «ЗАДАНИЕ» уезжало на третью.
    """
    keywords = tuple(str(k).strip().lower() for k in signature_keywords if str(k).strip())
    nonempty = [i for i, p in enumerate(paragraphs) if p.text.strip()]
    if not nonempty:
        return None
    last = nonempty[-1]
    if not _is_year_line(paragraphs[last].text):
        return None
    start = last
    if len(nonempty) >= 2:
        previous = nonempty[-2]
        previous_paragraph = paragraphs[previous]
        previous_text = previous_paragraph.text.strip()
        # Строку подписи в подвал не тянем: она относится к блоку подписей, а не
        # к «город + год», и распорка подвала ей не положена.
        is_signature = _paragraph_has_underline(previous_paragraph) or _matches_any(
            previous_text, keywords
        )
        if (
            len(previous_text) <= 40
            and not _is_year_line(previous_text)
            and not is_signature
        ):
            start = previous
    return start


def _structural_titles(preset: StylePreset) -> list[str]:
    """Ключевые слова структурных разделов из пресета (или значения по умолчанию)."""
    titles = getattr(preset.special_sections, "structural_titles", None)
    return list(titles) if titles else list(_DEFAULT_STRUCTURAL_TITLES)


def _is_structural_title(text: str, preset: StylePreset) -> bool:
    """True, если строка — заголовок структурного раздела (ВВЕДЕНИЕ, СПИСОК и т.п.)."""
    return _matches_any(text, _structural_titles(preset))


def _looks_like_toc_entry(text: str) -> bool:
    """True, если строка похожа на строку содержания «Заголовок …… 5»."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(re.match(r"^.*?[\s.\u2026]+\s*\d{1,4}\s*$", stripped))


def _upper_ratio(text: str) -> float:
    """Доля заглавных букв среди всех букв строки."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch.isupper()) / len(letters)


def _heading_shape_ok(stripped: str) -> bool:
    """Отсекает предложения и строки содержания, оставляя «форму» заголовка."""
    if not stripped or len(stripped) > 100:
        return False
    if _looks_like_toc_entry(stripped):
        return False
    if _upper_ratio(stripped) >= 0.6:
        return True
    # Не-заглавные строки: короткие и без завершающей «предложенческой» точки.
    # Двоеточие в конце тоже отметаем: «1. Содержание работы:» — это пункт
    # бланка задания, а не заголовок раздела с новой страницы.
    if stripped.endswith((".", "!", "?", "\u2026", ":")):
        return False
    return len(stripped.split()) <= _MAX_HEADING_WORDS


def _section_heading_level(
    text: str, preset: StylePreset, *, allow_numbered: bool = True
) -> int | None:
    """Уровень заголовка раздела (1..9) или ``None``.

    Распознаёт два вида заголовков:

    * структурный — «ВВЕДЕНИЕ», «РЕФЕРАТ», «СОДЕРЖАНИЕ», «ЗАКЛЮЧЕНИЕ»,
      «СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ» (по ключевым словам пресета);
    * нумерованный — «1 ГЛАВА. АНАЛИЗ…», «1. АНАЛИЗ…», «1.1 Описание…».
      Глубина номера задаёт уровень: «1» -> h1, «1.1» -> h2, «1.1.1» -> h3.

    Обычные предложения, строки содержания и длинные абзацы заголовками не
    считаются. ``allow_numbered=False`` оставляют только ключевые слова —
    это безопасно внутри специальных разделов, где «1. Содержание работы:»
    является пунктом бланка задания, а не заголовком раздела.
    """
    stripped = (text or "").strip()
    if not _heading_shape_ok(stripped):
        return None
    if _is_structural_title(stripped, preset):
        return 1
    if not allow_numbered:
        return None
    match = _SECTION_NUMBER_RE.match(stripped)
    if not match:
        return None
    parts = [part for part in match.group("number").split(".") if part]
    return max(1, min(len(parts), 9))


def _is_section_title_line(text: str, keywords: Sequence[str]) -> bool:
    """True, если строка — НАЗВАНИЕ специального раздела («РЕФЕРАТ», «ЗАДАНИЕ»).

    :func:`_matches_any` ищет ключевое слово где угодно в первых 60 символах,
    поэтому строка подписи «Задание приняла к исполнению … Петрова П.П.»
    распознавалась как начало раздела «ЗАДАНИЕ». Из-за этого граница титульного
    листа обрезалась на строке подписи, а блок «город + год» выпадал из титула
    и уезжал на вторую страницу.

    Здесь требование жёстче: строка должна быть похожа на заголовок (короткая,
    без завершающей точки) и либо совпадать с ключевым словом целиком, либо
    начинаться с него.
    """
    stripped = (text or "").strip()
    if not _heading_shape_ok(stripped):
        return False
    normalized = _normalize_match_text(stripped)
    if not normalized:
        return False
    for keyword in keywords:
        word = str(keyword or "").strip().lower()
        if not word or len(word) < _MIN_KEYWORD_LEN:
            continue
        if normalized == word or normalized.startswith(word + " "):
            return True
    return False


def _has_explicit_list_marker(text: str) -> bool:
    """True, если в НАЧАЛЕ строки исходника явно стоит маркер списка.

    Только явный маркер («1.», «2)», «а)», «-», «–», «•») даёт право считать
    абзац элементом списка. Если маркера нет, автоматическую нумерацию Word
    (``w:numPr``) снимаем: иначе в реферате после двоеточия появлялись цифры,
    которых в исходном документе не было.
    """
    return bool(_LIST_MARKER_RE.match((text or "").strip()))


def _split_list_marker(text: str) -> tuple[str, str] | None:
    """Разбирает строку на «маркер» и «текст без маркера».

    Возвращает ``None``, если маркера нет. Используется перед наложением маркера
    из конфигурации, чтобы исключить сдвоенные символы («- - пункт»).
    """
    match = _LEADING_MARKER_RE.match((text or "").strip())
    if not match:
        return None
    marker = match.group("marker").strip()
    rest = (match.group("rest") or "").strip()
    if not marker or not rest:
        return None
    return marker, rest


