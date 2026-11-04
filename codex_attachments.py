"""Resolve granted local attachments into official App Server user inputs."""
from pathlib import Path
import tempfile
import uuid
from urllib.parse import quote

from codex_engine import ProtocolError


def prepare_attachments(attachments, access, profile, upload_dir=None):
    from attach_tools import IMAGE_EXT, DOC_EXT, read_document
    from PIL import Image
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise ProtocolError("Некорректные вложения")
    root = Path(upload_dir or Path(tempfile.gettempdir()) / "kid_uploads").resolve()
    inputs, history = [], []
    images = 0
    remaining = 180000
    for item in attachments:
        try:
            if not isinstance(item, dict):
                raise ValueError()
            if item.get("file_id"):
                file_id = uuid.UUID(str(item["file_id"])).hex
                candidates = list(root.glob(file_id + "_*"))
                if len(candidates) != 1:
                    raise ValueError()
                path = candidates[0].resolve()
                if path.parent != root:
                    raise ValueError()
            else:
                path = Path(item.get("path") or "").resolve()
            if not access.allowed(str(path)) or not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
                raise ValueError()
        except (ValueError, OSError, TypeError):
            raise ProtocolError("Вложение недоступно. Прикрепите файл заново") from None
        suffix = path.suffix.lower()
        name = str(item.get("name") or path.name)
        if suffix in IMAGE_EXT:
            images += 1
            if images > max(1, min(20, int(profile.get("attach_max_images", 4)))):
                raise ProtocolError("Превышен лимит изображений")
            try:
                with Image.open(path) as image:
                    image.verify()
                # App Server reads the image locally; no URL fetch is needed.
                inputs.append({"type": "localImage", "path": str(path)})
            except Exception:
                raise ProtocolError("Не удалось прочитать изображение") from None
        else:
            # App Server accepts text and localImage, not arbitrary file blobs.
            # Give the agent the original path so its tools can inspect/edit any
            # format, including files that our optional text extractor cannot read.
            context = (
                f"Вложение: {name}\nЛокальный путь на ПК: {path}\n"
                "Исходный файл доступен для чтения и обработки локальными инструментами. "
                "Используй windows_command для чтения или редактирования файла на ПК. "
                "Выбирай способ чтения по содержимому и формату, а не только расширению. "
                "Если предпросмотра нет или он неполный, прочитай оригинал инструментами. "
                "Не утверждай, что прочитал файл, до получения его содержимого.\n"
            )
            if suffix in DOC_EXT and remaining > 0:
                text, _, error = read_document(str(path), max_text_chars=min(60000, remaining))
                if not error and text:
                    text = text[:min(60000, remaining)]
                    remaining -= len(text)
                    context += "Предпросмотр содержимого (может быть неполным):\n" + text
                else:
                    context += "Предпросмотр недоступен; прочитай исходный файл инструментами."
            inputs.append({"type": "text", "text": context})
        history.append({"name": name, "url": "/api/download-local?path=" + quote(str(path), safe=""),
                        "isImage": suffix in IMAGE_EXT, "isVideo": False})
    return inputs, history
