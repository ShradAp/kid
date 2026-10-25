"""Проверки доступа к локальному мосту и явно выбранным файлам."""
from __future__ import annotations

import hmac
import secrets
import threading
from pathlib import Path

BRIDGE_TOKEN = secrets.token_urlsafe(32)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 128 * 1024 * 1024


def authorized(headers, port: int) -> bool:
    return (
        headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}
        and hmac.compare_digest(headers.get("X-Kid-Token", "").encode("utf-8"), BRIDGE_TOKEN.encode("ascii"))
        and not headers.get("Origin")
    )


def retained_secret(value, existing="", clear=False) -> str:
    if clear is True:
        return ""
    return str(value).strip() if value else str(existing or "")


class FileAccess:
    """Разрешены файлы вложений/результатов и выбор пользователя в диалоге."""

    def __init__(self, roots=()):
        self.roots = tuple(Path(root).resolve() for root in roots)
        self._selected = set()
        self._lock = threading.Lock()

    def grant(self, path: str) -> str:
        target = Path(path).resolve()
        if not target.is_file():
            raise FileNotFoundError("Selected file does not exist")
        with self._lock:
            self._selected.add(target)
        return str(target)

    def allowed(self, path: str) -> bool:
        if not path:
            return False
        try:
            target = Path(path).resolve()
            if not target.is_file():
                return False
            with self._lock:
                if target in self._selected:
                    return True
            return any(target.is_relative_to(root) for root in self.roots)
        except (OSError, ValueError):
            return False


def request_length(headers, upload=False) -> int:
    if headers.get("Transfer-Encoding"):
        raise ValueError("Transfer-Encoding is not supported")
    values = headers.get_all("Content-Length", [])
    if len(values) > 1:
        raise ValueError("Duplicate Content-Length")
    raw = headers.get("Content-Length", "0")
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("Invalid Content-Length")
    length = int(raw)
    if length > (MAX_UPLOAD_BYTES if upload else MAX_JSON_BYTES):
        raise OverflowError("Request is too large")
    return length
