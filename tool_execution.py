"""Shared validation, cancellation and durable de-duplication for kid tools."""
import hashlib
import json
from pathlib import Path
import threading


class ToolExecutionError(RuntimeError):
    pass


def execute_tool(name, arguments, handlers, cancel=None):
    if cancel is not None and cancel.is_set():
        raise ToolExecutionError("Действие отменено")
    if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 2 * 1024 * 1024:
        raise ToolExecutionError("Некорректные аргументы инструмента")
    handler = handlers.get(name)
    if handler is None:
        raise ToolExecutionError("Инструмент недоступен в выбранном режиме")
    result = handler(**arguments)
    if cancel is not None and cancel.is_set():
        raise ToolExecutionError("Действие отменено; результат не повторяется автоматически")
    return result


class ExecutionJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()

    def execute(self, operation, name, arguments, callback):
        identity = hashlib.sha256(json.dumps([operation, name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (identity + ".json")
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("status") == "completed":
                    return saved["result"]
                raise ToolExecutionError("Исход действия неизвестен; повторное исполнение заблокировано")
            path.write_text('{"status":"started"}', encoding="utf-8")
            result = callback()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"status": "completed", "result": result}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            return result
