"""Bounded local file reception and immutable snapshots for Telegram delivery."""

import hashlib
from pathlib import Path
import re
import uuid

from codex_engine import ProtocolError

DOWNLOAD_LIMIT = 20 * 1024 * 1024
UPLOAD_LIMIT = 50 * 1024 * 1024


def filename(value):
    name = str(value or "attachment.bin").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:160]
    return name or "attachment.bin"


class TelegramFiles:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.incoming = self.directory / "telegram" / "files" / "incoming"
        self.outgoing = self.directory / "telegram" / "files" / "outgoing"
        from services.telegram_archive import TelegramArchive
        self.archive = TelegramArchive(directory)

    def receive(self, attachment, transport, cancel, profile, settings=None, metadata=None):
        if int(attachment.get("file_size") or 0) > DOWNLOAD_LIMIT:
            raise ProtocolError("Входящий файл превышает 20 МБ")
        info = transport.call("getFile", file_id=attachment["file_id"])
        if int(info.get("file_size") or 0) > DOWNLOAD_LIMIT:
            raise ProtocolError("Входящий файл превышает 20 МБ")
        name = filename(attachment.get("file_name") or info.get("file_path"))
        self.incoming.mkdir(parents=True, exist_ok=True)
        destination = self.incoming / (uuid.uuid4().hex + "_" + name)
        transport.download(info["file_path"], destination, cancel, DOWNLOAD_LIMIT)
        archived = self.archive.register(settings, destination, name, attachment, metadata) if settings else {}
        inputs, details = self.prepare(destination, name, profile)
        if archived:
            details["archive_id"] = archived["archive_id"]
            inputs.append({"type": "text", "text": "Persistent Telegram archive ID: " + archived["archive_id"]})
        return inputs, details

    def prepare(self, destination, name, profile):
        from bridge_security import FileAccess
        from codex_attachments import prepare_attachments
        inputs, _ = prepare_attachments(
            [{"path": str(destination), "name": name}], FileAccess((self.incoming,)), profile)
        return inputs, {"name": name, "path": str(destination), "size": destination.stat().st_size}

    def source(self, path):
        if str(path).startswith(("\\\\", "//")):
            raise ProtocolError("Для отправки нужен локальный файл, сетевые пути не поддерживаются")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ProtocolError("Для отправки файла нужен абсолютный путь на компьютере")
        candidate = candidate.resolve()
        if not candidate.is_file() or candidate.stat().st_size > UPLOAD_LIMIT:
            raise ProtocolError("Файл недоступен или превышает 50 МБ")
        permitted_private = (self.incoming.resolve(), (self.directory / "generated_files").resolve())
        if candidate.is_relative_to(self.directory) and not any(candidate.is_relative_to(root) for root in permitted_private):
            raise ProtocolError("Служебные файлы, токены и история KID не отправляются через Telegram")
        return candidate

    def snapshot(self, source, cancel):
        self.outgoing.mkdir(parents=True, exist_ok=True)
        target = self.outgoing / (uuid.uuid4().hex + "_" + filename(source.name))
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as original, target.open("xb") as saved:
                while chunk := original.read(64 * 1024):
                    if cancel.is_set():
                        raise ProtocolError("Отправка файла остановлена")
                    size += len(chunk)
                    if size > UPLOAD_LIMIT:
                        raise ProtocolError("Файл превышает 50 МБ")
                    digest.update(chunk)
                    saved.write(chunk)
            return {"kind": "document", "id": uuid.uuid4().hex, "path": str(target),
                    "filename": filename(source.name), "size": size, "sha256": digest.hexdigest()}
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def validate_snapshot(self, entry):
        path = Path(entry["path"]).resolve()
        if not path.is_relative_to(self.outgoing.resolve()) or not path.is_file():
            raise ProtocolError("Исходящий файл Telegram недоступен")
        if path.stat().st_size != entry["size"] or path.stat().st_size > UPLOAD_LIMIT:
            raise ProtocolError("Исходящий файл Telegram изменён")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise ProtocolError("Исходящий файл Telegram изменён")
        return path
