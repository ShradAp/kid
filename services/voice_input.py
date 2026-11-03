"""Local, opt-in microphone dictation. No audio is stored or sent over the network."""
from __future__ import annotations

import collections
import json
from pathlib import Path
import queue
import re
import threading
import time

START_COMMANDS = ("начать запись", "начни запись", "кид диктуй")
STOP_COMMANDS = ("конец записи", "закончить запись", "остановить запись", "кид стоп")
MODEL_NAME = "vosk-model-small-ru-0.22"


def normalized(text):
    return " ".join(re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")))


def command_position(text, commands):
    found = []
    for command in commands:
        match = re.search(r"(?<!\w)" + re.escape(command) + r"(?!\w)", text)
        if match:
            found.append((match.start(), match.end()))
    return min(found) if found else None


class VoiceInput:
    def __init__(self, data_dir):
        self.model_path = Path(data_dir) / "voice_models" / MODEL_NAME
        self.lock = threading.RLock()
        self.control = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.model = None
        self.enabled = False
        self.state = "off"
        self.error = ""
        self.partial = ""
        self.session_id = ""
        self.sequence = 0
        self.events = collections.deque(maxlen=256)
        self.last_poll = time.monotonic()

    def _snapshot_locked(self, session_id, after):
        owned = session_id == self.session_id
        return {
            "enabled": self.enabled and owned,
            "state": self.state if owned else "off",
            "error": self.error if owned else "",
            "partial": self.partial if owned else "",
            "sequence": self.sequence,
            "events": [dict(item) for item in self.events
                       if owned and item["id"] > after],
            "other_session": self.enabled and not owned,
        }

    def snapshot(self, session_id, after=0):
        with self.lock:
            if session_id == self.session_id:
                self.last_poll = time.monotonic()
            return self._snapshot_locked(session_id, after)

    def configure(self, session_id, enabled, after=0):
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
            raise ValueError("Не указан чат для диктовки")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть логическим значением")
        with self.control:
            with self.lock:
                if not enabled and self.session_id != session_id:
                    return self._snapshot_locked(session_id, after)
                same = enabled and self.enabled and self.session_id == session_id
            if same:
                return self.snapshot(session_id, after)
            self.stop_event.set()
            previous = self.thread
            if previous is not None:
                previous.join(timeout=4)
                if previous.is_alive():
                    raise RuntimeError("Микрофон ещё останавливается. Повтори через несколько секунд.")
            with self.lock:
                self.enabled = False
                self.state = "off"
                self.partial = ""
                if enabled:
                    if not (self.model_path / "am" / "final.mdl").is_file():
                        raise RuntimeError("Русская модель не установлена. Запусти scripts/setup_voice_input.py.")
                    self.session_id = session_id
                    self.events.clear()
                    self.error = ""
                    self.enabled = True
                    self.state = "starting"
                    self.last_poll = time.monotonic()
                    self.stop_event = threading.Event()
                    self.thread = threading.Thread(target=self._run, daemon=True, name="kid-voice-input")
                    self.thread.start()
            return self.snapshot(session_id, after)

    def close(self):
        with self.control:
            self.stop_event.set()
            if self.thread is not None:
                self.thread.join(timeout=4)
            with self.lock:
                self.enabled = False
                self.state = "off"
                self.partial = ""

    def _append_locked(self, text):
        text = text.strip()
        if text:
            self.sequence += 1
            self.events.append({"id": self.sequence, "text": text})

    def consume_text(self, text):
        """Apply reserved commands to a final recognition result."""
        text = normalized(text)
        with self.lock:
            self.partial = ""
            if not self.enabled or not text:
                return
            if self.state == "waiting":
                start = command_position(text, START_COMMANDS)
                if start is None:
                    return
                self.state = "recording"
                text = text[start[1]:].strip()
            if self.state == "recording":
                stop = command_position(text, STOP_COMMANDS)
                if stop is not None:
                    self._append_locked(text[:stop[0]])
                    self.state = "waiting"
                else:
                    self._append_locked(text)

    def _run(self):
        recognizer = None
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            if self.model is None:
                self.model = Model(str(self.model_path))
            if self.stop_event.is_set():
                return
            sample_rate = int(sd.query_devices(kind="input")["default_samplerate"])
            recognizer = KaldiRecognizer(self.model, sample_rate)
            audio = queue.Queue(maxsize=40)

            def callback(data, frames, timing, status):
                if self.stop_event.is_set():
                    return
                try:
                    audio.put_nowait(bytes(data))
                except queue.Full:
                    # Drop old audio rather than building an unbounded delay.
                    try:
                        audio.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        audio.put_nowait(bytes(data))
                    except queue.Full:
                        pass

            with sd.RawInputStream(samplerate=sample_rate, blocksize=max(800, sample_rate // 10),
                                   channels=1, dtype="int16", callback=callback):
                with self.lock:
                    self.state = "waiting"
                while not self.stop_event.is_set():
                    with self.lock:
                        # A vanished UI must not leave a hidden microphone running.
                        if time.monotonic() - self.last_poll > 300:
                            break
                    try:
                        chunk = audio.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(chunk):
                        self.consume_text(json.loads(recognizer.Result()).get("text", ""))
                    else:
                        partial = json.loads(recognizer.PartialResult()).get("partial", "")
                        with self.lock:
                            self.partial = partial if self.state == "recording" else ""
                self.consume_text(json.loads(recognizer.FinalResult()).get("text", ""))
        except Exception as exc:
            with self.lock:
                self.error = "Не удалось включить диктовку: " + str(exc)
        finally:
            with self.lock:
                self.enabled = False
                self.state = "off"
                self.partial = ""


def get_voice_input():
    """Lazy singleton: importing the HTTP bridge never opens a microphone."""
    global _instance
    with _instance_lock:
        if _instance is None:
            from app_paths import USER_DATA_DIR
            _instance = VoiceInput(USER_DATA_DIR)
        return _instance


def close_voice_input():
    if _instance is not None:
        _instance.close()


def handle_get(handler):
    from urllib.parse import parse_qs, urlsplit
    params = parse_qs(urlsplit(handler.path).query)
    try:
        after = max(0, int(params.get("after", ["0"])[0]))
        result = get_voice_input().snapshot(params.get("session_id", [""])[0], after)
        status = 200
    except (ValueError, TypeError) as exc:
        result, status = {"error": str(exc)}, 400
    handler._set_cors(status)
    handler.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def handle_post(handler):
    try:
        body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0))) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Некорректный запрос")
        result = get_voice_input().configure(body.get("session_id"), body.get("enabled"),
                                            max(0, int(body.get("after", 0))))
        status = 200
    except (ValueError, TypeError) as exc:
        result, status = {"error": str(exc)}, 400
    except RuntimeError as exc:
        result, status = {"error": str(exc)}, 409
    handler._set_cors(status)
    handler.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


_instance = None
_instance_lock = threading.Lock()
