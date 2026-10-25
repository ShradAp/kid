# Модуль вложений: изображения, полные видеофайлы, PDF, таблицы, текст.
# Возвращает медиа как data-URL и текстовый контекст для промпта.

import base64
import csv
import io
import os
import subprocess
import tempfile
import threading

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".heic", ".heif")
VIDEO_EXT = (
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".mkv",
    ".webm",
    ".m4v",
    ".3gp",
)
DOC_EXT = (
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".ini",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".c",
    ".cpp",
    ".cs",
    ".java",
    ".sql",
    ".bat",
    ".ps1",
    ".xlsx",
    ".xls",
)

MAX_SIDE = 1024  # картинка ужимается до этого размера (было 1400: base64 300-500КБ на кадр)
JPEG_QUALITY = 70  # было 82: экономия ~40% base64 без потери читаемости для vision
MAX_TEXT_CHARS = 60000  # предел текста с одного документа (≈20-30 страниц Word)
MAX_TEXT_PER_FILE_IN_PAYLOAD = 60000  # сколько символов одного файла реально уходит в LLM
MAX_IMAGES_PER_PAYLOAD = 4  # больше 4 кадров/картинок за раз не отправляем
MAX_TOTAL_TEXT_IN_PAYLOAD = 180000  # общий потолок на все текстовые вложения за раз
WHISPER_MODEL = os.environ.get("KID_WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("KID_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("KID_WHISPER_COMPUTE_TYPE", "int8")

_LOG = None
_WHISPER_MODEL = None
_WHISPER_LOCK = threading.Lock()


def _log(msg):
    if _LOG:
        try:
            _LOG(msg)
        except Exception:
            pass


def _num(name, default):
    """Числовой лимит из окружения: desktop_app кладёт его из профиля перед
    вызовом build_payload/read_document. Если переменной нет или она невалидна —
    возвращается default."""
    raw = os.environ.get(name, "")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return default


def _limits():
    """Текущие лимиты вложений. desktop_app кладёт их в os.environ из профиля
    перед каждым вызовом build_payload — значения применяются в реальном времени
    без перезапуска. Если переменных нет, используются дефолтные константы выше."""
    return {
        "max_images": _num("ATTACH_MAX_IMAGES", MAX_IMAGES_PER_PAYLOAD),
        "max_text_chars": _num("ATTACH_MAX_TEXT_CHARS", MAX_TEXT_CHARS),
        "max_text_per_file": _num("ATTACH_MAX_TEXT_PER_FILE", MAX_TEXT_PER_FILE_IN_PAYLOAD),
        "max_total_text": _num("ATTACH_MAX_TOTAL_TEXT", MAX_TOTAL_TEXT_IN_PAYLOAD),
        "csv_max_rows": _num("ATTACH_CSV_MAX_ROWS", 400),
        "xlsx_max_rows": _num("ATTACH_XLSX_MAX_ROWS", 400),
    }


# ------------------------------------------------------------------ картинки


def _pil():
    from PIL import Image

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    return Image


def image_to_dataurl(path_or_img):
    # Ужимает картинку и кодирует в base64 data-URL
    Image = _pil()
    if isinstance(path_or_img, str):
        img = Image.open(path_or_img)
    else:
        img = path_or_img
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + b64


image_to_data_uri = image_to_dataurl


# --------------------------------------------------------------------- видео


def _get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        with _WHISPER_LOCK:
            if _WHISPER_MODEL is None:
                from faster_whisper import WhisperModel

                _log("Загружаю faster-whisper (%s, %s, %s)" % (
                    WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE
                ))
                _WHISPER_MODEL = WhisperModel(
                    WHISPER_MODEL,
                    device=WHISPER_DEVICE,
                    compute_type=WHISPER_COMPUTE_TYPE,
                )
    return _WHISPER_MODEL


def transcribe_video(path, status_cb=None):
    """Извлекает аудиодорожку локально и возвращает её расшифровку.

    Видео никогда не кодируется в payload: ffmpeg создаёт временный WAV, который
    сразу читает faster-whisper и затем удаляется.
    """
    audio_path = None
    try:
        if status_cb:
            status_cb("Извлекаю аудио из %s" % os.path.basename(path))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio_file:
            audio_path = audio_file.name
        command = [
            "ffmpeg", "-y", "-v", "error", "-i", path,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path,
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            return "", "не удалось извлечь аудио (требуется ffmpeg): %s" % (detail or "неизвестная ошибка")
        if status_cb:
            status_cb("Транскрибирую %s локально" % os.path.basename(path))
        segments, info = _get_whisper_model().transcribe(audio_path, vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        if not text:
            return "", "аудиодорожка не содержит распознаваемой речи"
        language = getattr(info, "language", None)
        language_note = ", язык: %s" % language if language else ""
        return text, "расшифровано%s" % language_note
    except ImportError:
        return "", "не установлена faster-whisper (pip install faster-whisper)"
    except Exception as ex:
        _log("transcribe_video: %s" % ex)
        return "", "ошибка транскрибации: %s" % ex
    finally:
        if audio_path:
            try:
                os.remove(audio_path)
            except OSError:
                pass


# ---------------------------------------------------------------- документы


def _read_xlsx(path):
    try:
        import openpyxl
    except Exception:
        return "[нет openpyxl (pip install openpyxl)]"
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append("--- лист: %s ---" % ws.title)
            n = 0
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                if not any(v.strip() for v in vals):
                    continue
                parts.append(" | ".join(vals))
                n += 1
                if n > _num("ATTACH_XLSX_MAX_ROWS", 400):
                    parts.append("[...лист обрезан...]")
                    break
        return "\n".join(parts)
    except Exception as ex:
        return "[ошибка XLSX: %s]" % ex


def _read_csv(path):
    try:
        rows = []
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except Exception:
                dialect = csv.excel
            max_rows = _num("ATTACH_CSV_MAX_ROWS", 400)
            for i, row in enumerate(csv.reader(f, dialect)):
                rows.append(" | ".join(row))
                if i > max_rows:
                    rows.append("[...файл обрезан...]")
                    break
        return "\n".join(rows)
    except Exception as ex:
        return "[ошибка CSV: %s]" % ex


def read_document(path, max_text_chars=None):
    """
    Чтение документов и исходного кода с обрезкой под max_text_chars
    (по умолчанию берётся из окружения/константы), иначе один лог на 2МБ
    уходит в LLM целиком и переотправляется в истории.
    """
    if max_text_chars is None:
        max_text_chars = _limits()["max_text_chars"]
    ext = os.path.splitext(path)[1].lower()

    # 1. Список текстовых расширений и исходного кода
    text_extensions = {
        ".txt",
        ".py",
        ".json",
        ".js",
        ".html",
        ".css",
        ".md",
        ".yaml",
        ".yml",
        ".xml",
        ".ini",
        ".cfg",
        ".bat",
        ".sh",
        ".ps1",
        ".log",
        ".sql",
    }

    if ext in text_extensions:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_text_chars + 1)
            truncated = len(content) > max_text_chars
            if truncated:
                content = content[:max_text_chars] + "\n...(файл обрезан до лимита)..."

            lines_count = content.count("\n") + (1 if content else 0)
            return content, lines_count, None
        except Exception as e:
            return "", 0, f"Ошибка чтения {os.path.basename(path)}: {e}"

    if ext in (".csv", ".tsv"):
        try:
            content = _read_csv(path)
            if ext == ".tsv":
                content = content.replace(",", " | ")
            if len(content) > max_text_chars:
                content = content[:max_text_chars] + "\n...(таблица обрезана до лимита)..."
            return content, content.count("\n") + (1 if content else 0), None
        except Exception as e:
            return "", 0, f"Ошибка чтения таблицы: {e}"

    if ext in (".xlsx", ".xls"):
        content = _read_xlsx(path)
        if content.startswith("[") and content.endswith("]"):
            return "", 0, content
        if len(content) > max_text_chars:
            content = content[:max_text_chars] + "\n...(таблица обрезана до лимита)..."
        return content, content.count("\n") + (1 if content else 0), None

    # 2. Обработка PDF / DOCX (если модули установлены)
    if ext == ".pdf":
        try:
            import pypdf

            reader = pypdf.PdfReader(path)
            pages_text = []
            total = 0
            for p in reader.pages:
                t = p.extract_text() or ""
                if total + len(t) > max_text_chars:
                    t = t[: max(0, max_text_chars - total)] + "\n...(PDF обрезан до лимита)..."
                    pages_text.append(t)
                    break
                pages_text.append(t)
                total += len(t)
            full_text = "\n".join(pages_text)
            return full_text, len(reader.pages), None
        except Exception as e:
            return "", 0, f"Ошибка PDF: {e}"

    if ext in (".docx", ".doc"):
        try:
            import docx

            doc = docx.Document(path)
            parts = []
            total = 0
            for p in doc.paragraphs:
                if not p.text.strip():
                    continue
                if total + len(p.text) > max_text_chars:
                    parts.append(p.text[: max(0, max_text_chars - total)] + "\n...(DOCX обрезан)...")
                    break
                parts.append(p.text)
                total += len(p.text)
            full_text = "\n".join(parts)
            return full_text, len(doc.paragraphs), None
        except Exception as e:
            return "", 0, f"Ошибка DOCX: {e}"

    # Для неизвестных бинарных форматов
    return "", 0, f"Формат {ext} не поддерживается как текст"


# ------------------------------------------------------------ сборка payload


def build_payload(attachments, status_cb=None):
    images = []
    text_blocks = []
    notes = []
    total_chars = 0

    _lim = _limits()
    max_images = _lim["max_images"]
    max_text_per_file = _lim["max_text_per_file"]
    max_total_text = _lim["max_total_text"]

    for path in attachments:
        if not os.path.exists(path):
            notes.append(f"Файл не найден: {os.path.basename(path)}")
            continue

        filename = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()

        # Картинки: не больше max_images за один запрос,
        # иначе base64 на мегабайты уходит в каждый следующий вопрос истории
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            if len(images) >= max_images:
                notes.append(f"{filename} (пропущен: лимит {max_images} изображений за раз)")
                continue
            if status_cb:
                status_cb(f"Кодирую {filename}")
            try:
                data_uri = image_to_data_uri(path)
                images.append(data_uri)
                notes.append(f"{filename} (изображение)")
            except Exception as e:
                notes.append(f"{filename} (ошибка: {e})")
            continue

        # Видео локально превращается в текст: модель получает только транскрипцию.
        if ext in VIDEO_EXT:
            if status_cb:
                status_cb(f"Транскрибирую видео {filename}")
            transcript, transcript_note = transcribe_video(path, status_cb=status_cb)
            if transcript:
                if len(transcript) > max_text_per_file:
                    transcript = transcript[:max_text_per_file] + "\n...(транскрипция обрезана до лимита вложений)..."
                block = (
                    f"--- НАЧАЛО ТРАНСКРИПЦИИ {filename} ---\n"
                    f"{transcript}\n"
                    f"--- КОНЕЦ ТРАНСКРИПЦИИ {filename} ---"
                )
                if total_chars + len(block) > max_total_text:
                    notes.append(f"{filename} (транскрипция пропущена: превышен общий лимит вложений)")
                else:
                    text_blocks.append(block)
                    total_chars += len(block)
                    notes.append(f"{filename} ({transcript_note})")
            else:
                notes.append(f"{filename} ({transcript_note})")
            continue

        # Текстовые документы и скрипты
        if status_cb:
            status_cb(f"Читаю {filename}")

        content, count, err = read_document(path)
        if err:
            notes.append(f"{filename} ({err})")
        elif content:
            size_kb = os.path.getsize(path) / 1024
            if len(content) > max_text_per_file:
                content = content[:max_text_per_file] + "\n...(файл обрезан до лимита для экономии токенов)..."
            header = f"--- НАЧАЛО ФАЙЛА {filename} ({count} строк) ---"
            footer = f"--- КОНЕЦ ФАЙЛА {filename} ---"
            block = f"{header}\n{content}\n{footer}"
            # Общий потолок на все файлы, чтобы 10 вложений не дали сотни тысяч символов
            if total_chars + len(block) > max_total_text:
                notes.append(f"{filename} (пропущен: превышен общий лимит вложений)")
                continue
            text_blocks.append(block)
            total_chars += len(block)

            notes.append(f"{filename}: прочитано {count} строк ({size_kb:.1f} КБ)")
        else:
            notes.append(f"{filename} (файл пуст)")

    full_text = "\n\n".join(text_blocks)
    return images, full_text, notes
