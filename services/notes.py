"""Atomic notes persistence shared by UI, API tools and Codex host tools."""

import json
import os
import threading


class NoteStore:
    def __init__(self, path, log):
        self.path = str(path)
        self.log = log
        self.lock = threading.RLock()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except Exception as e:
                self.log(f"[!] Ошибка чтения notes.json: {e}")
        return []


    def save(self, notes):
        temporary_file = self.path + f".{os.getpid()}.{threading.get_ident()}.tmp"
        with self.lock:
            try:
                with open(temporary_file, "w", encoding="utf-8") as f:
                    json.dump(notes, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary_file, self.path)
            except Exception as e:
                self.log(f"[!] Ошибка записи notes.json: {e}")
                raise OSError("Не удалось сохранить заметки") from None
            finally:
                try:
                    os.unlink(temporary_file)
                except OSError:
                    pass


    def prepend(self, note):
        with self.lock:
            notes = self.load()
            notes.insert(0, note)
            self.save(notes)

