"""Attachment upload and download endpoints; shared file access stays injected."""

import email.parser
import email.policy
import json
import os
import re
import shutil
import tempfile
import subprocess
import sys
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog
import urllib.parse
import uuid

import file_tools
from app_paths import relocated_user_file


def _local_link_path(reference: str) -> str:
    """Normalize Markdown/file-URI paths without interpreting them as commands."""
    path = reference.strip()
    if path.startswith("<") and path.endswith(">"):
        path = path[1:-1]
    if path.lower().startswith("file://"):
        uri = urllib.parse.urlsplit(path)
        if uri.netloc and uri.netloc.lower() != "localhost":
            raise ValueError("Ссылка должна указывать на локальный файл")
        path = urllib.parse.unquote(uri.path)
    else:
        path = urllib.parse.unquote(path)
    # Codex file references may carry a source line/column or #L12C3 fragment.
    path = re.sub(r"#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?$", "", path)
    path = re.sub(r":\d+(?::\d+)?$", "", path)
    if re.match(r"^/[a-zA-Z]:[/\\]", path):
        path = path[1:]
    if not path or any(ord(char) < 32 for char in path) or not os.path.isabs(path):
        raise ValueError("Требуется абсолютный путь к файлу или папке")
    return os.path.normpath(path)


def reveal_local(request):
    """Reveal a user-clicked path; never execute the referenced file."""
    try:
        data = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))))
        if not isinstance(data, dict) or not isinstance(data.get("path"), str):
            raise ValueError("Не указан путь к файлу")
        path = _local_link_path(data["path"])
        if not os.path.exists(path):
            request._set_cors(404)
            request.wfile.write(json.dumps({"error": "Файл или папка не найдены"}, ensure_ascii=False).encode("utf-8"))
            return
        if os.name == "nt":
            # One quoted /select argument keeps spaces, commas and parentheses
            # in the filename intact. No shell or file association is invoked.
            explorer = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
            argument = f'/select,"{path}"' if os.path.isfile(path) else f'"{path}"'
            subprocess.Popen(f'"{explorer}" {argument}', shell=False)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
        request._set_cors()
        request.wfile.write(b'{"ok":true}')
    except (ValueError, OSError) as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))


def _content_disposition(filename: str) -> str:
    """Заголовок Content-Disposition для имени файла (в т.ч. кириллического).

    HTTP-заголовки кодируются latin-1, поэтому кириллица в имени рушила
    ``send_header`` (UnicodeEncodeError) и соединение обрывалось — браузер
    показывал «Failed to fetch»/ERR_EMPTY_RESPONSE. Даём ASCII-фолбэк в
    ``filename`` и корректное имя по RFC 5987 в ``filename*``.
    """
    safe = str(filename or "download").replace('"', "_").replace("\\", "_")
    ascii_name = safe.encode("ascii", "ignore").decode("ascii").strip() or "download"
    quoted = urllib.parse.quote(safe, safe="")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quoted)


def _download_url(path: str, name: str) -> str:
    """Ссылка на скачивание локального файла через мост."""
    return "/api/download-local?path=%s&name=%s" % (
        urllib.parse.quote(path),
        urllib.parse.quote(name),
    )


def download_local(request, file_access):
    query = urllib.parse.urlparse(request.path).query
    params = urllib.parse.parse_qs(query)
    requested_path = relocated_user_file(params.get("path", [""])[0])
    requested_name = os.path.basename(params.get("name", ["attachment"])[0]) or "attachment"
    if not file_access.allowed(requested_path):
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
        return
    try:
        file_size = os.path.getsize(requested_path)
        request.send_response(200)
        request.send_header("Content-Type", "application/octet-stream")
        request.send_header("Content-Length", str(file_size))
        request.send_header("Content-Disposition", _content_disposition(requested_name))
        request.end_headers()
        with open(requested_path, "rb") as download_file:
            shutil.copyfileobj(download_file, request.wfile)
    except OSError:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл недоступен"}, ensure_ascii=False).encode("utf-8"))
    return


def codex_image(request, user_data_dir):
    from codex_images import image_file
    identity = urllib.parse.urlsplit(request.path).path.removeprefix("/api/codex-images/")
    path = image_file(user_data_dir, identity)
    if path is None:
        request.send_error(404)
        return
    try:
        with path.open("rb") as stream:
            request.send_response(200)
            request.send_header("Content-Type", {".png": "image/png", ".jpg": "image/jpeg",
                                             ".webp": "image/webp", ".gif": "image/gif"}[path.suffix])
            request.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
            request.send_header("X-Content-Type-Options", "nosniff")
            request.end_headers()
            shutil.copyfileobj(stream, request.wfile)
    except OSError:
        request.send_error(404)
    return


def download_upload(request):
    file_id = request.path.split("/api/download/", 1)[1].split("?", 1)[0]
    upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
    matches = [name for name in os.listdir(upload_dir)] if os.path.isdir(upload_dir) else []
    matched = next((name for name in matches if name.startswith(file_id + "_")), None)
    if not matched:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
        return
    stored_path = os.path.join(upload_dir, matched)
    try:
        file_size = os.path.getsize(stored_path)
        safe_name = matched.split("_", 1)[1]
        request.send_response(200)
        request.send_header("Content-Type", "application/octet-stream")
        request.send_header("Content-Length", str(file_size))
        request.send_header("Content-Disposition", _content_disposition(safe_name))
        request.end_headers()
        with open(stored_path, "rb") as download_file:
            shutil.copyfileobj(download_file, request.wfile)
    except OSError:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл недоступен"}, ensure_ascii=False).encode("utf-8"))
    return


def generated_preview(request):
    file_id = request.path.split("/api/generated/", 1)[1].split("/", 1)[0]
    preview = file_tools.preview_generated_file(file_id)
    if not preview:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
        return
    request._set_cors()
    request.wfile.write(json.dumps({"ok": True, **preview}, ensure_ascii=False).encode("utf-8"))
    return


def upload(request):
    content_length = int(request.headers.get("Content-Length", 0))
    if content_length > 512 * 1024 * 1024:
        request._set_cors(413)
        request.wfile.write(json.dumps({"error": "Размер файла превышает 512 МБ"}, ensure_ascii=False).encode("utf-8"))
        return
    content_type = request.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": "Ожидается multipart/form-data"}, ensure_ascii=False).encode("utf-8"))
        return
    raw_body = request.rfile.read(content_length)
    header_message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8") + raw_body
    )
    # iter_attachments() НЕЛЬЗЯ использовать: он считает первую text/*-часть
    # «телом письма» и пропускает её — txt/md/py/csv-файлы (и файлы без
    # явного Content-Type) не находились и вложение молча отклонялось.
    upload_part = None
    first_payload_part = None
    for part in header_message.walk():
        if part.is_multipart():
            continue
        payload_bytes = part.get_payload(decode=True)
        if payload_bytes is None:
            continue
        if first_payload_part is None:
            first_payload_part = part
        if part.get_filename():
            upload_part = part
            break
    if upload_part is None:
        upload_part = first_payload_part
    if upload_part is None:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
        return
    original_name = upload_part.get_filename() or "attachment"
    safe_name = os.path.basename(original_name).replace("\x00", "") or "attachment"
    upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    file_id = uuid.uuid4().hex
    stored_path = os.path.join(upload_dir, file_id + "_" + safe_name)
    with open(stored_path, "wb") as uploaded_file:
        uploaded_file.write(upload_part.get_payload(decode=True))
    request._set_cors()
    request.wfile.write(json.dumps({"file_id": file_id, "name": safe_name, "mime": upload_part.get_content_type()}, ensure_ascii=False).encode("utf-8"))
    return



def _generated_file(file_id: str, file_access) -> tuple[str | None, str | None]:
    """Путь и имя для скачивания файла, созданного file_tools.create_downloadable_file.

    Возвращает (полный_путь, имя_для_скачивания) либо (None, None), если файл
    по переданному id (rab://download/<id>) не найден.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return None, None
    try:
        index = file_tools._load_index()
    except Exception:  # noqa: BLE001 - битый index.json не роняет выдачу файла
        return None, None
    entry = index.get(file_id) if isinstance(index, dict) else None
    if not isinstance(entry, dict):
        return None, None
    stored_name = str(entry.get("stored_name") or "")
    download_name = str(entry.get("download_name") or "") or stored_name
    if not stored_name:
        return None, None
    path = os.path.join(file_tools.GENERATED_DIR, stored_name)
    if not file_access.allowed(path):
        return None, None
    return path, download_name


def _uploaded_document_path(reference: str, file_access) -> str | None:
    """Путь к загруженному документу: абсолютный путь либо file_id из /api/upload."""
    reference = relocated_user_file((reference or "").strip())
    if not reference:
        return None
    if file_access.allowed(reference):
        return reference
    upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
    if os.path.isdir(upload_dir):
        for name in os.listdir(upload_dir):
            if name == reference or name.startswith(reference + "_"):
                candidate = os.path.join(upload_dir, name)
                if os.path.isfile(candidate):
                    return candidate
    return None


def _unique_target_path(directory: str, filename: str) -> str:
    """Несуществующий путь для сохранения файла в directory.

    При конфликте имени добавляет « (1)», « (2)», … перед расширением, чтобы
    авто-скачивание не затирало уже существующие файлы.
    """
    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    for idx in range(1, 1000):
        candidate = os.path.join(directory, f"{stem} ({idx}){ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(directory, f"{stem}_{int(time.time())}{ext}")


def _pick_directory_native(initialdir: str) -> str:
    """Открывает системный диалог выбора папки в отдельном процессе.

    Диалог показывается в дочернем процессе на его ГЛАВНОМ потоке — это изолирует
    tkinter от потоков HTTP-сервера и от уже запущенного GUI родителя (pywebview/
    WebView2/CLR), где диалог в фоновом потоке не выходил на передний план либо
    ронял запрос. Результат передаётся через временный файл (pythonw без stdout).
    """
    import subprocess
    import tempfile

    script = (
        "import sys\n"
        "from tkinter import Tk, filedialog\n"
        "root = Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        "root.update()\n"
        "d = filedialog.askdirectory(title='Выберите папку для скачивания', initialdir=sys.argv[1])\n"
        "root.destroy()\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write(d or '')\n"
    )
    fd, result_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        subprocess.run(
            [sys.executable, "-c", script, initialdir or "", result_path],
            timeout=300,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(result_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass


def get_desktop_dir():
    user_home = os.path.expanduser("~")
    for name in ["Desktop", "Рабочий стол"]:
        p = os.path.join(user_home, name)
        if os.path.exists(p):
            return p
    return user_home


def download_generated(request, generated_file):
    file_id = request.path.split("/api/generated/", 1)[1].split("?", 1)[0]
    gen_path, gen_name = generated_file(file_id)
    if not gen_path:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
        return
    try:
        file_size = os.path.getsize(gen_path)
        request.send_response(200)
        request.send_header("Content-Type", "application/octet-stream")
        request.send_header("Content-Length", str(file_size))
        request.send_header("Content-Disposition", _content_disposition(gen_name or "file"))
        request.end_headers()
        with open(gen_path, "rb") as generated_file:
            shutil.copyfileobj(generated_file, request.wfile)
    except OSError:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл недоступен"}, ensure_ascii=False).encode("utf-8"))
    return


def open_generated(request, generated_file):
    file_id = request.path.split("/api/generated/", 1)[1].split("/", 1)[0]
    gen_path, _gen_name = generated_file(file_id)
    if not gen_path:
        request._set_cors(404)
        request.wfile.write(json.dumps({"error": "Файл не найден"}, ensure_ascii=False).encode("utf-8"))
        return
    if Path(gen_path).suffix.lower() not in {".docx", ".txt", ".md", ".csv", ".pdf", ".xlsx"}:
        request._set_cors(403)
        request.wfile.write(b'{"error":"This file type cannot be opened automatically"}')
        return
    try:
        if os.name == "nt":
            os.startfile(gen_path)
        else:
            subprocess.Popen(["xdg-open", gen_path])
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return


def save_file(request, uploaded_document_path, generated_file, load_config, log_event):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body) if body else {}
        if not isinstance(req_data, dict):
            raise ValueError("Некорректный запрос сохранения")
        reference = str(req_data.get("path") or req_data.get("file_id") or "")
        source_path = uploaded_document_path(reference)
        if not source_path:
            # Файл, созданный моделью (rab://download/<id>) — лежит в generated_files/.
            generated_path, _generated_name = generated_file(reference)
            source_path = generated_path
        if not source_path:
            raise FileNotFoundError("Файл для сохранения не найден")
        suggested = os.path.basename(
            str(req_data.get("name") or os.path.basename(source_path))
        ) or "document.docx"

        # Базовый путь скачивания из настроек: если задан — сохраняем туда
        # сразу, без диалога «Сохранить как». При сбое падаем на диалог.
        base_dir = os.path.expanduser(
            str(load_config().get("download_base_path", "") or "").strip()
        )
        if base_dir:
            try:
                os.makedirs(base_dir, exist_ok=True)
                auto_path = _unique_target_path(base_dir, suggested)
                shutil.copyfile(source_path, auto_path)
                log_event(f"[SAVE] {source_path} -> {auto_path}")
                request._set_cors()
                request.wfile.write(
                    json.dumps(
                        {"ok": True, "path": auto_path, "auto": True},
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
                return
            except Exception as save_error:  # noqa: BLE001 - падаем на диалог
                log_event(f"[!] Авто-сохранение не удалось, открываю диалог: {save_error}")

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            chosen = filedialog.asksaveasfilename(
                title="Сохранить документ",
                initialdir=get_desktop_dir(),
                initialfile=suggested,
                defaultextension=os.path.splitext(suggested)[1] or ".docx",
            )
        finally:
            root.destroy()

        if not chosen:
            request._set_cors()
            request.wfile.write(
                json.dumps({"ok": True, "canceled": True}, ensure_ascii=False).encode("utf-8")
            )
            return
        shutil.copyfile(source_path, chosen)
        log_event(f"[SAVE] {source_path} -> {chosen}")
        request._set_cors()
        request.wfile.write(
            json.dumps({"ok": True, "path": chosen}, ensure_ascii=False).encode("utf-8")
        )
    except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
        log_event(f"[!] /api/save-file: {exc}")
        request._set_cors(400)
        request.wfile.write(
            json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
        )
    return


def pick_files(request, file_access):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected_paths = filedialog.askopenfilenames(
        initialdir=get_desktop_dir(),
        title="Выберите файлы для отправки",
        filetypes=[
            (
                "Все поддерживаемые",
                "*.png *.jpg *.jpeg *.webp *.mp4 *.webm *.mov *.mkv *.txt *.py *.md *.json *.pdf *.docx *.xlsx *.csv",
            ),
            ("Видео", "*.mp4 *.webm *.mov *.mkv"),
            ("Все файлы", "*.*"),
        ],
    )
    root.destroy()

    files_info = []
    for p in selected_paths:
        p = file_access.grant(p)
        files_info.append(
            {"path": p, "name": os.path.basename(p), "size": os.path.getsize(p)}
        )

    request._set_cors()
    request.wfile.write(
        json.dumps({"files": files_info}, ensure_ascii=False).encode("utf-8")
    )
    return


def pick_directory(request, pick_directory_native, log_event):
    try:
        chosen_dir = pick_directory_native(get_desktop_dir())
        request._set_cors()
        request.wfile.write(
            json.dumps({"path": chosen_dir or ""}, ensure_ascii=False).encode("utf-8")
        )
    except Exception as exc:  # noqa: BLE001 — диалог не должен ронять запрос
        log_event(f"[!] /api/pick-directory: {exc}")
        request._set_cors(500)
        request.wfile.write(
            json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
        )
    return


