"""Persist image tool outputs separately from chat JSON and serve local previews."""
import base64
import binascii
import hashlib
import io
from pathlib import Path
import re

MAX_IMAGE_BYTES = 32 * 1024 * 1024
FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
           "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}


def image_file(directory, identity):
    if not re.fullmatch(r"[a-f0-9]{64}\.(png|jpg|webp|gif)", identity):
        return None
    root = (Path(directory) / "generated_files" / "codex_images").resolve()
    path = root / identity
    if path.is_file() and path.resolve().is_relative_to(root):
        return path
    return None


def collect_images(directory, item, roots=()):
    """Only inspect output blocks, never tool arguments or input/reference images."""
    from PIL import Image
    kind = item.get("type")
    sources = []
    if kind == "functionCallOutput" and isinstance(item.get("output"), list):
        sources = [part.get("image_url") for part in item["output"]
                   if isinstance(part, dict) and part.get("type") == "input_image"]
    elif kind == "mcpToolCall" and isinstance(item.get("result"), dict):
        sources = ["data:" + str(part.get("mimeType")) + ";base64," + str(part.get("data"))
                   for part in item["result"].get("content", [])
                   if isinstance(part, dict) and part.get("type") == "image"]
    elif kind == "dynamicToolCall":
        sources = [part.get("imageUrl") for part in item.get("contentItems") or []
                   if isinstance(part, dict) and part.get("type") == "inputImage"]
    elif kind == "imageGeneration" and item.get("status") == "completed":
        value = item.get("result")
        if isinstance(value, str) and value:
            sources = [value if value.startswith("data:") else "data:image/png;base64," + value]
        elif item.get("savedPath"):
            try:
                raw_path = item["savedPath"]
                # Reject network paths before filesystem resolution.
                if not isinstance(raw_path, str) or raw_path.startswith(("\\\\", "//")):
                    return []
                path = Path(raw_path).resolve()
                if any(path.is_relative_to(Path(root).resolve()) for root in roots) and path.is_file():
                    if path.stat().st_size <= MAX_IMAGE_BYTES:
                        sources = [path.read_bytes()]
            except (ValueError, OSError):
                return []
    images = []
    for source in sources:
        try:
            if isinstance(source, bytes):
                data = source
            else:
                if not isinstance(source, str) or len(source) > MAX_IMAGE_BYTES * 4 // 3 + 100:
                    continue
                match = re.fullmatch(r"data:image/(?:png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)", source)
                if not match:
                    continue
                data = base64.b64decode(match[1].replace("\n", "").replace("\r", ""), validate=True)
            if len(data) > MAX_IMAGE_BYTES:
                continue
            with Image.open(io.BytesIO(data)) as image:
                extension, mime = FORMATS[image.format]
                image.verify()
            identity = hashlib.sha256(data).hexdigest() + "." + extension
            folder = Path(directory) / "generated_files" / "codex_images"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / identity
            if not path.exists():
                path.write_bytes(data)
            entry = {"name": "image-" + identity[:12] + "." + extension,
                     "url": "/api/codex-images/" + identity, "mime": mime}
            if entry not in images:
                images.append(entry)
        except (ValueError, KeyError, OSError, binascii.Error, Image.DecompressionBombError):
            continue
    return images
