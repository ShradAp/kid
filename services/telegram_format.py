"""Markdown -> safe Telegram rich HTML plus a readable plain-text fallback."""

from html import escape
import re
from urllib.parse import urlsplit

from services.telegram_transport import text_chunks


def _math_delimiters(source):
    # Math normalization must not alter literal code or escaped backticks.
    code = re.compile(r"(`{3,}[^\n]*\n[\s\S]*?\n`{3,}[^\n]*|~{3,}[^\n]*\n[\s\S]*?\n~{3,}[^\n]*|`+[^`\n]+`+)")
    def normalize(value):
        value = re.sub(r"\\\[([\s\S]*?)\\\]", lambda m: "\n$$\n" + m[1].strip() + "\n$$\n", value)
        value = re.sub(r"\\\(([^\n]*?)\\\)", lambda m: "$" + m[1].strip() + "$", value)
        return re.sub(r"\$\$([\s\S]*?)\$\$", lambda m: "\n$$\n" + m[1].strip() + "\n$$\n", value)
    pieces, offset = [], 0
    for match in code.finditer(source):
        pieces.extend((normalize(source[offset:match.start()]), match[0]))
        offset = match.end()
    pieces.append(normalize(source[offset:]))
    return "".join(pieces)


def _literal(tokens):
    return "".join(_literal(token["children"]) if token.get("children") else
                   "\n" if token["type"] in ("softbreak", "linebreak") else str(token.get("raw") or "") for token in tokens)


def _inline(tokens, depth=0):
    html, plain = [], []
    for token in tokens:
        kind = token["type"]
        raw = str(token.get("raw") or "")
        children = token.get("children") or []
        inner, text = _inline(children, depth + 1) if children and depth < 12 else (escape(_literal(children) if children else raw), _literal(children) if children else raw)
        if kind in ("text", "inline_html"):
            inner, text = escape(raw), raw
        elif kind in ("softbreak", "linebreak"):
            inner, text = "<br>", "\n"
        elif kind == "codespan":
            inner, text = "<code>" + escape(raw) + "</code>", raw
        elif kind == "inline_math":
            inner, text = "<tg-math>" + escape(raw) + "</tg-math>", raw
        elif kind in ("strong", "emphasis", "strikethrough"):
            tag = {"strong": "b", "emphasis": "i", "strikethrough": "s"}[kind]
            inner = f"<{tag}>{inner}</{tag}>"
        elif kind in ("link", "image"):
            url = str((token.get("attrs") or {}).get("url") or "")
            try:
                safe = urlsplit(url).scheme.lower() in ("https", "http", "mailto", "tel")
            except ValueError:
                safe = False
            if kind == "link" and safe:
                inner = '<a href="' + escape(url, quote=True) + '">' + inner + "</a>"
            elif url:
                # Never embed remote media or turn a model-supplied local path into a URL.
                inner += " (" + escape(url) + ")"
            if url and url != text:
                text += " (" + url + ")"
        html.append(inner)
        plain.append(text)
    return "".join(html), "".join(plain)


def _block(token, depth=0):
    kind = token["type"]
    raw = str(token.get("raw") or "")
    attrs = token.get("attrs") or {}
    children = token.get("children") or []
    if kind == "blank_line":
        return "", ""
    if kind in ("paragraph", "block_text", "heading"):
        html, text = _inline(children)
        tag = "h" + str(max(1, min(6, int(attrs.get("level", 2))))) if kind == "heading" else "p"
        return f"<{tag}>{html}</{tag}>", text + "\n\n"
    if kind in ("block_code", "block_math"):
        info = str(attrs.get("info") or "").split()
        language = info[0] if info else ""
        if kind == "block_math" or language in ("math", "latex", "tex"):
            return "<tg-math-block>" + escape(raw.strip()) + "</tg-math-block>", raw.strip() + "\n\n"
        language = re.sub(r"[^A-Za-z0-9_+-]", "", language)[:30]
        code = '<code class="language-' + language + '">' if language else "<code>"
        return "<pre>" + code + escape(raw) + "</code></pre>", raw.rstrip("\n") + "\n\n"
    if kind == "table":
        rows = []
        for section in children:
            rows.extend([section] if section["type"] == "table_head" else section.get("children", []))
        html_rows, plain_rows = [], []
        for row in rows:
            html_cells, text_cells = [], []
            for cell in row.get("children", []):
                value, text = _inline(cell.get("children", []))
                cell_attrs = cell.get("attrs") or {}
                tag = "th" if cell_attrs.get("head") else "td"
                align = cell_attrs.get("align") or "left"
                align = align if align in ("left", "center", "right") else "left"
                html_cells.append(f'<{tag} align="{align}">{value}</{tag}>')
                text_cells.append(text)
            html_rows.append("<tr>" + "".join(html_cells) + "</tr>")
            plain_rows.append(" | ".join(text_cells))
        text = "\n".join(plain_rows) + "\n\n"
        if any(len(row.get("children", [])) > 20 for row in rows):
            return "<pre>" + escape(text.rstrip()) + "</pre>", text
        return "<table bordered striped compact>" + "".join(html_rows) + "</table>", text
    if kind in ("list", "list_item", "task_list_item", "block_quote") and depth < 10:
        parts = [_block(child, depth + 1) for child in children]
        html = "".join(item[0] for item in parts)
        text = "".join(item[1] for item in parts)
        if kind == "list":
            ordered = attrs.get("ordered")
            tag = "ol" if ordered else "ul"
            start = f' start="{int(attrs.get("start", 1))}"' if ordered else ""
            return f"<{tag}{start}>{html}</{tag}>", text + "\n"
        if kind in ("list_item", "task_list_item"):
            prefix = ("☑ " if attrs.get("checked") else "☐ ") if kind == "task_list_item" else "• "
            return "<li>" + escape(prefix if kind == "task_list_item" else "") + html + "</li>", prefix + text.strip() + "\n"
        return "<blockquote>" + html + "</blockquote>", text + "\n"
    if kind == "thematic_break":
        return "<hr/>", "────────\n\n"
    if children:
        html, text = _inline(children)
        return "<p>" + html + "</p>", text + "\n\n"
    return "<p>" + escape(raw) + "</p>", raw + "\n\n"


def format_reply(source):
    """Each result is balanced HTML and <=3500 UTF-16 units of visible text.

    Rich chunks remain small enough to downgrade one-for-one to sendMessage.
    Very large individual blocks degrade to plain text without losing content.
    """
    import mistune

    parser = mistune.create_markdown(renderer="ast", plugins=["table", "math", "strikethrough", "task_lists"])
    tokens = parser(_math_delimiters(str(source or "Ответ без текста")))
    current_html, current_text = "", ""
    produced = False
    for token in tokens:
        html, text = _block(token)
        if not text.strip():
            continue
        if len(text.encode("utf-16-le")) // 2 > 3500 or len(html) > 24000 or html.count("<") > 300:
            if current_html:
                yield {"text": current_text.rstrip(), "rich_html": current_html}
                current_html, current_text = "", ""
                produced = True
            # A split can end in a significant space or newline inside code.
            # Preserve such boundaries exactly instead of trimming each chunk.
            for part in text_chunks(text.rstrip("\n")):
                yield {"text": part, "rich_html": "<pre>" + escape(part) + "</pre>"}
                produced = True
            continue
        else:
            pieces = [(html, text)]
        for html, text in pieces:
            if current_html and (len((current_text + text).encode("utf-16-le")) // 2 > 3500
                                 or len(current_html + html) > 24000 or (current_html + html).count("<") > 300):
                yield {"text": current_text.rstrip(), "rich_html": current_html}
                produced = True
                current_html, current_text = "", ""
            current_html += html
            current_text += text
    if current_text.strip():
        yield {"text": current_text.rstrip(), "rich_html": current_html}
    elif not produced:
        yield {"text": "Ответ без текста"}
