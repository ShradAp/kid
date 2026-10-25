"""Инструменты для создания скачиваемых файлов."""

import io
import json
import os
import re
import time
import uuid
import zipfile
from collections.abc import Callable
from xml.sax.saxutils import escape as xml_escape

from app_paths import USER_DATA_DIR, log_error

DOWNLOAD_PREFIX = "rab://download/"
GENERATED_DIR = str(USER_DATA_DIR / "generated_files")
INDEX_PATH = os.path.join(GENERATED_DIR, "index.json")

_LOGGER: Callable[[str], None] | None = None

_DOWNLOAD_FORMAT_EXT = {"txt": ".txt", "py": ".py", "docx": ".docx"}
_FORMAT_ALIAS = {"text": "txt", "python": "py", "doc": "docx", "markdown": "md"}
_MARKDOWN_MODES = {"auto", "keep", "strip"}

_INLINE_MD_RE = re.compile(
    r"(<u>.*?</u>|\*\*\*[^*\n]+?\*\*\*|\*\*[^*\n]+?\*\*|__[^_\n]+?__|"
    r"~~[^~\n]+?~~|`[^`\n]+`|\*[^*\n]+?\*|_[^_\n]+?_)",
    re.IGNORECASE,
)


def _log(text: str) -> None:
    try:
        if _LOGGER is not None:
            _LOGGER(text)
        else:
            log_error(text)
    except Exception:
        pass


def _ensure_dir() -> None:
    os.makedirs(GENERATED_DIR, exist_ok=True)


def _load_index() -> dict:
    if not os.path.exists(INDEX_PATH):
        return {}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        _log("file_tools: index read error: %s" % e)
        return {}


def _save_index(index: dict) -> None:
    _ensure_dir()
    try:
        with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        _log("file_tools: index write error: %s" % e)


def _safe_filename(name: str, default_ext: str = ".txt") -> str:
    s = (name or "").strip().replace("\\", "/").split("/")[-1]
    s = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "file" + default_ext

    root, ext = os.path.splitext(s)
    ext = re.sub(r"[^A-Za-z0-9._-]+", "", ext)
    if not ext:
        ext = default_ext
    if len(ext) > 12:
        ext = ext[:12]

    root = root.strip(" ._") or "file"
    if len(root) > 80:
        root = root[:80].rstrip(" ._")
    return root + ext


def _normalize_format(
    file_format: str, file_name: str, allowed_ext: dict, default_fmt: str
) -> str:
    fmt = (file_format or "").strip().lower().lstrip(".")
    fmt = _FORMAT_ALIAS.get(fmt, fmt)
    if fmt in allowed_ext:
        return fmt

    ext = os.path.splitext(file_name)[1].lower().lstrip(".")
    ext = _FORMAT_ALIAS.get(ext, ext)
    if ext in allowed_ext:
        return ext

    return default_fmt


def _apply_format_to_filename(file_name: str, fmt: str, ext_map: dict) -> str:
    ext = ext_map.get(fmt, ".txt")
    root, _ = os.path.splitext(file_name)
    root = root.strip(" ._") or "file"
    return root + ext


def _normalize_markdown_mode(markdown_mode: str) -> str:
    mode = (markdown_mode or "").strip().lower()
    return mode if mode in _MARKDOWN_MODES else "auto"


def _normalize_content(content) -> str:
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _unwrap_single_fence(text: str) -> str:
    lines = text.split("\n")
    if len(lines) < 2:
        return text
    if re.match(r"^\s*(```|~~~)", lines[0]) and re.match(
        r"^\s*(```|~~~)\s*$", lines[-1]
    ):
        return "\n".join(lines[1:-1])
    return text


def _md_links_to_text(text: str) -> str:
    def rep_img(m):
        alt = (m.group(1) or "").strip()
        url = (m.group(2) or "").strip()
        if alt and url:
            return "%s (%s)" % (alt, url)
        return alt or url

    def rep_link(m):
        label = (m.group(1) or "").strip()
        url = (m.group(2) or "").strip()
        if not url:
            return label
        if not label or label == url:
            return url
        return "%s (%s)" % (label, url)

    out = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", rep_img, text)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", rep_link, out)
    return out


def _strip_inline_markers(text: str) -> str:
    s = text
    s = re.sub(r"<u>(.*?)</u>", r"\1", s, flags=re.IGNORECASE | re.DOTALL)
    for pat in (
        (r"\*\*\*([^*\n]+?)\*\*\*", r"\1"),
        (r"\*\*([^*\n]+?)\*\*", r"\1"),
        (r"__([^_\n]+?)__", r"\1"),
        (r"~~([^~\n]+?)~~", r"\1"),
        (r"\*([^*\n]+?)\*", r"\1"),
        (r"_([^_\n]+?)_", r"\1"),
        (r"`([^`\n]+?)`", r"\1"),
    ):
        s = re.sub(pat[0], pat[1], s)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", s)
    return s


def _markdown_to_plain(text: str) -> str:
    s = _normalize_content(text)
    s = _unwrap_single_fence(s)
    s = _md_links_to_text(s)
    s = re.sub(r"(?im)^\s*(```|~~~).*?$", "", s)
    s = re.sub(r"(?im)^\s{0,3}#{1,6}\s*", "", s)
    s = re.sub(r"(?im)^\s*>\s?", "", s)
    s = re.sub(r"(?im)^\s*[-*+]\s+", "• ", s)
    s = re.sub(r"(?im)^\s*\d+[.)]\s+", "", s)
    s = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "", s)
    s = _strip_inline_markers(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip("\n")


def _parse_inline_runs(line: str, enable_markdown: bool) -> list[tuple[str, dict]]:
    if not line:
        return []
    if not enable_markdown:
        return [(line, {})]

    out = []
    pos = 0
    for m in _INLINE_MD_RE.finditer(line):
        if m.start() > pos:
            out.append((line[pos : m.start()], {}))

        tok = m.group(0)
        style = {}
        txt = tok

        low = tok.lower()
        if low.startswith("<u>") and low.endswith("</u>"):
            txt = tok[3:-4]
            style["underline"] = True
        elif tok.startswith("***") and tok.endswith("***"):
            txt = tok[3:-3]
            style["bold"] = True
            style["italic"] = True
        elif tok.startswith("**") and tok.endswith("**"):
            txt = tok[2:-2]
            style["bold"] = True
        elif tok.startswith("__") and tok.endswith("__"):
            # В этом инструменте '__text__' трактуем как подчёркнутый текст.
            txt = tok[2:-2]
            style["underline"] = True
        elif tok.startswith("~~") and tok.endswith("~~"):
            txt = tok[2:-2]
            style["strike"] = True
        elif tok.startswith("`") and tok.endswith("`"):
            txt = tok[1:-1]
            style["code"] = True
        elif (
            tok.startswith("*")
            and tok.endswith("*")
            or tok.startswith("_")
            and tok.endswith("_")
        ):
            txt = tok[1:-1]
            style["italic"] = True

        txt = _strip_inline_markers(txt)
        if txt:
            out.append((txt, style))
        pos = m.end()

    if pos < len(line):
        out.append((line[pos:], {}))
    return out


def _run_xml(
    text: str,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    strike: bool = False,
    code: bool = False,
    size: int | None = None,
) -> str:
    if text == "":
        return ""

    props = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if underline:
        props.append('<w:u w:val="single"/>')
    if strike:
        props.append("<w:strike/>")
    if size is not None:
        props.append('<w:sz w:val="%d"/>' % int(size))
        props.append('<w:szCs w:val="%d"/>' % int(size))
    if code:
        props.append(
            '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
        )

    rpr = "<w:rPr>%s</w:rPr>" % "".join(props) if props else ""
    esc = xml_escape(text, {'"': "&quot;"})
    return '<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, esc)


def _line_to_docx_runs(line: str, markdown_mode: str) -> list[tuple[str, dict]]:
    work = line
    base = {}

    if markdown_mode == "auto":
        m_h = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", work)
        if m_h:
            lvl = min(len(m_h.group(1)), 6)
            size_by_lvl = {1: 36, 2: 32, 3: 28, 4: 24, 5: 22, 6: 20}
            base["bold"] = True
            base["size"] = size_by_lvl.get(lvl, 24)
            work = m_h.group(2).strip()
        else:
            m_ul = re.match(r"^\s*[-*+]\s+(.*)$", work)
            m_ol = re.match(r"^\s*\d+[.)]\s+(.*)$", work)
            if m_ul:
                work = "• " + (m_ul.group(1) or "")
            elif m_ol:
                work = "• " + (m_ol.group(1) or "")

    work = _md_links_to_text(work)
    runs = _parse_inline_runs(work, enable_markdown=(markdown_mode == "auto"))
    out = []
    for txt, st in runs:
        if not txt:
            continue
        merged = dict(base)
        merged.update(st)
        out.append((txt, merged))
    return out


def _paragraph_xml(line: str, markdown_mode: str) -> str:
    if line == "":
        return "<w:p/>"
    runs = _line_to_docx_runs(line, markdown_mode)
    if not runs:
        return "<w:p/>"
    body = ""
    for txt, st in runs:
        body += _run_xml(
            txt,
            bold=bool(st.get("bold")),
            italic=bool(st.get("italic")),
            underline=bool(st.get("underline")),
            strike=bool(st.get("strike")),
            code=bool(st.get("code")),
            size=st.get("size"),
        )
    return "<w:p>%s</w:p>" % body


def _make_docx_bytes(text: str, markdown_mode: str = "auto") -> bytes:
    """Минимальный .docx без внешних библиотек. Для auto: markdown -> форматирование."""
    mode = _normalize_markdown_mode(markdown_mode)
    src = _normalize_content(text)

    if mode in ("auto", "strip"):
        src = _unwrap_single_fence(src)
    if mode == "strip":
        src = _markdown_to_plain(src)

    paras = [_paragraph_xml(line, mode) for line in src.split("\n")]
    body = "".join(paras)

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + body + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
        'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return out.getvalue()


def _normalize_text_for_format(text: str, fmt: str, markdown_mode: str) -> str:
    mode = _normalize_markdown_mode(markdown_mode)
    src = _normalize_content(text)

    if fmt == "txt":
        # УСЛОВИЯ для txt по умолчанию:
        # auto => не сохраняем markdown-разметку, сохраняем читаемый plain-text.
        if mode in ("auto", "strip"):
            return _markdown_to_plain(src)
        return src

    if fmt == "py":
        if mode in ("auto", "strip"):
            return _unwrap_single_fence(src)
        return src

    if fmt == "md":
        if mode == "strip":
            return _markdown_to_plain(src)
        return src

    return src


def _write_file(path: str, text: str, fmt: str, markdown_mode: str) -> None:
    if fmt == "docx":
        payload = _make_docx_bytes(text, markdown_mode)
        with open(path, "wb") as fh:
            fh.write(payload)
        return

    body = _normalize_text_for_format(text, fmt, markdown_mode)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


def create_downloadable_file(
    file_name: str,
    content,
    purpose: str = "",
    file_format: str = "",
    markdown_mode: str = "auto",
) -> dict:
    """Создаёт файл для скачивания и возвращает rab://download/<id>."""
    try:
        text = _normalize_content(content)
        if not text.strip():
            return {
                "ok": False,
                "error": "Пустой content: передай в tool только нужный фрагмент текста.",
            }

        safe_name = _safe_filename(file_name, default_ext=".txt")
        fmt = _normalize_format(file_format, safe_name, _DOWNLOAD_FORMAT_EXT, "txt")
        safe_name = _apply_format_to_filename(safe_name, fmt, _DOWNLOAD_FORMAT_EXT)
        mode = _normalize_markdown_mode(markdown_mode)

        file_id = uuid.uuid4().hex[:16]
        ext = _DOWNLOAD_FORMAT_EXT.get(fmt, ".txt")
        stored_name = file_id + ext

        _ensure_dir()
        src_path = os.path.join(GENERATED_DIR, stored_name)
        _write_file(src_path, text, fmt, mode)

        size_bytes = 0
        try:
            size_bytes = int(os.path.getsize(src_path))
        except Exception:
            pass

        entry = {
            "id": file_id,
            "stored_name": stored_name,
            "download_name": safe_name,
            "size_bytes": size_bytes,
            "created_at": int(time.time()),
            "purpose": (purpose or "").strip(),
            "format": fmt,
            "markdown_mode": mode,
        }
        idx = _load_index()
        idx[file_id] = entry
        _save_index(idx)

        dl_url = DOWNLOAD_PREFIX + file_id
        return {
            "ok": True,
            "file_id": file_id,
            "file_name": safe_name,
            "size_bytes": size_bytes,
            "file_format": fmt,
            "markdown_mode": mode,
            "download_url": dl_url,
            "download_markdown": "[Скачать файл «%s»](%s)" % (safe_name, dl_url),
            "hint": (
                "TXT по умолчанию сохраняется как plain-text (без markdown), "
                "для сохранения сырого markdown передай markdown_mode='keep'."
            ),
        }
    except Exception as e:
        _log("file_tools: create file failed: %s" % e)
        return {"ok": False, "error": "Ошибка создания файла: %s" % e}


def preview_generated_file(file_id: str) -> dict | None:
    """Возвращает {name, format, content} для предпросмотра созданного файла.

    Для docx текст извлекается из word/document.xml, для txt/py/md читается
    как обычный текст. Если файл не найден — None.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return None
    idx = _load_index()
    entry = idx.get(file_id)
    if not isinstance(entry, dict):
        return None
    stored_name = str(entry.get("stored_name") or "")
    download_name = str(entry.get("download_name") or "") or stored_name
    fmt = str(entry.get("format") or "txt")
    if not stored_name:
        return None
    path = os.path.join(GENERATED_DIR, stored_name)
    if not os.path.isfile(path):
        return None
    return {"name": download_name, "format": fmt, "content": _read_file_text(path, fmt)}


def _read_file_text(path: str, fmt: str) -> str:
    if fmt == "docx":
        try:
            with zipfile.ZipFile(path) as zf:
                xml_bytes = zf.read("word/document.xml")
            xml = xml_bytes.decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
            return (
                text.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&apos;", "'")
            )
        except Exception:
            return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""
