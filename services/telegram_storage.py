"""Private Telegram data, separate from desktop chat history and repository files."""

import copy
import json
import os
from pathlib import Path
import threading
import uuid


DEFAULT_SETTINGS = {"enabled": False, "token": "", "owner_id": "", "profile_id": "", "network": "direct",
                    "live_status": True, "streaming": True, "notifications": True}


class TelegramStorage:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.settings_path = self.directory / "telegram.json"
        self.history_path = self.directory / "telegram" / "history" / "chat.json"
        self.lock = threading.RLock()

    @staticmethod
    def read(path, default):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Некорректное хранилище Telegram")
            return value
        except FileNotFoundError:
            return copy.deepcopy(default)

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def settings(self):
        with self.lock:
            return {**DEFAULT_SETTINGS, **self.read(self.settings_path, {})}

    def save_settings(self, settings):
        with self.lock:
            self.write(self.settings_path, settings)

    def state(self):
        with self.lock:
            return self.read(self.history_path, {
                "session_id": "telegram_" + uuid.uuid4().hex, "history": [],
                "inbox": [], "outbox": [], "offset": 0, "active": None,
            })

    def change(self, callback):
        with self.lock:
            state = self.state()
            result = callback(state)
            self.write(self.history_path, state)
            return result
