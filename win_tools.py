"""Инструменты фонового управления Windows для LLM.

Все операции выполняются через PowerShell БЕЗ всплывающих окон и без эмуляции
кликов мыши (subprocess с флагом CREATE_NO_WINDOW, PowerShell -NonInteractive).
Доступ: файловые операции, чтение/правка реестра, автозапуск, контроль процессов.

КОНТУР БЕЗОПАСНОСТИ (обязательный): ни одна команда не исполняется молча.
Перед выполнением операция паркуется в очереди с точным текстом команды,
описанием назначения и последствиями. Выполнение стартует ТОЛЬКО после явного
подтверждения человеком через approve(); reject() отменяет. Модель не имеет
никакого пути выполнить команду в обход approve().
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid

# Максимальное время ожидания подтверждения (сек), после которого операция отменяется.
APPROVAL_TIMEOUT_SEC = 600

_lock = threading.Lock()
_pending: dict[str, dict] = {}
_events: dict[str, threading.Event] = {}
_decisions: dict[str, str] = {}

# Разрешённые типы значений реестра (защита от инъекции в -Type).
_REG_VALUE_TYPES = ("String", "ExpandString", "DWord", "QWord", "Binary", "MultiString")


def _ps_quote(value) -> str:
    """Экранирует значение для одинарных кавычек PowerShell (литеральная строка)."""
    return "'" + str(value).replace("'", "''") + "'"


def _norm_reg_key(key: str) -> str:
    """Приводит путь реестра к виду PS-провайдера: HKCU -> HKCU:\\..."""
    k = str(key or "").strip()
    for hive in ("HKCC", "HKCR", "HKCU", "HKLM", "HKU"):
        if k.upper().startswith(hive):
            if k[: len(hive) + 1].upper() != hive + ":":
                k = hive + ":" + k[len(hive):]
            break
    return k


def _run_ps(command: str, timeout: int = 120) -> dict:
    """Выполняет PowerShell без окна. Возвращает {ok, rc, stdout, stderr}."""
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command", command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": f"таймаут {timeout} с"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(exc)}


def _request_approval(spec: dict) -> str:
    """Блокирует выполнение до решения человека. Возвращает 'approve'/'reject'."""
    action_id = uuid.uuid4().hex[:12]
    event = threading.Event()
    with _lock:
        _pending[action_id] = {**spec, "id": action_id, "status": "pending"}
        _events[action_id] = event
    event.wait(timeout=APPROVAL_TIMEOUT_SEC)
    with _lock:
        decision = _decisions.pop(action_id, "reject")
        _events.pop(action_id, None)
        _pending.pop(action_id, None)
    return decision


def approve(action_id: str) -> bool:
    """Подтверждает операцию и разблокирует ожидающий вызов."""
    with _lock:
        event = _events.get(action_id)
        if event is None:
            return False
        _decisions[action_id] = "approve"
        item = _pending.get(action_id)
        if item:
            item["status"] = "approve"
    event.set()
    return True


def reject(action_id: str) -> bool:
    """Отклоняет операцию и разблокирует ожидающий вызов."""
    with _lock:
        event = _events.get(action_id)
        if event is None:
            return False
        _decisions[action_id] = "reject"
        item = _pending.get(action_id)
        if item:
            item["status"] = "reject"
    event.set()
    return True


def list_pending() -> list:
    """Список операций, ожидающих подтверждения (для интерфейса)."""
    with _lock:
        return [dict(v) for v in _pending.values() if v.get("status") == "pending"]


def build_action(action: str, **params) -> dict:
    """Строит спецификацию операции: точная команда + описание + последствия.

    Возвращает {operation, command, description, consequences} либо {error}.
    Команда НЕ выполняется здесь — только собирается для показа пользователю.
    """
    a = (action or "").strip().lower()
    target = str(params.get("target") or params.get("path") or "").strip()
    value = str(params.get("value") or params.get("content") or "")
    name = str(params.get("name") or "").strip()

    def spec(operation, command, description, consequences):
        return {
            "operation": operation,
            "command": command,
            "description": description,
            "consequences": consequences,
        }

    # ---------- файловые операции ----------
    if a in ("list_dir", "list"):
        if not target:
            return {"error": "не указан путь к папке"}
        return spec(
            "Список файлов",
            f"Get-ChildItem -Path {_ps_quote(target)} -Force | Select-Object Mode, Length, LastWriteTime, Name | Format-Table -AutoSize | Out-String -Width 200",
            f"Показать содержимое папки «{target}»",
            "Только чтение, ничего не изменяет.",
        )

    if a == "read_file":
        if not target:
            return {"error": "не указан путь к файлу"}
        return spec(
            "Чтение файла",
            f"Get-Content -Path {_ps_quote(target)} -Raw -ErrorAction Stop",
            f"Прочитать содержимое файла «{target}»",
            "Только чтение, ничего не изменяет.",
        )

    if a == "write_file":
        if not target:
            return {"error": "не указан путь к файлу"}
        escaped = value.replace("'", "''")
        return spec(
            "Запись файла",
            f"Set-Content -Path {_ps_quote(target)} -Value '{escaped}' -Encoding UTF8 -Force",
            f"Записать (перезаписать) файл «{target}»",
            "Перезапишет существующий файл или создаст новый.",
        )

    if a in ("delete", "delete_path"):
        if not target:
            return {"error": "не указан путь"}
        return spec(
            "Удаление",
            f"Remove-Item -Path {_ps_quote(target)} -Recurse -Force",
            f"Удалить «{target}»",
            "НЕОБРАТИМО: удалит файл или папку со всем содержимым.",
        )

    if a == "copy":
        dst = str(params.get("destination") or "").strip()
        if not target or not dst:
            return {"error": "нужны исходный путь и путь назначения"}
        return spec(
            "Копирование",
            f"Copy-Item -Path {_ps_quote(target)} -Destination {_ps_quote(dst)} -Recurse -Force",
            f"Скопировать «{target}» → «{dst}»",
            "Создаст копию; существующий файл в назначении будет перезаписан.",
        )

    if a == "move":
        dst = str(params.get("destination") or "").strip()
        if not target or not dst:
            return {"error": "нужны исходный путь и путь назначения"}
        return spec(
            "Перемещение",
            f"Move-Item -Path {_ps_quote(target)} -Destination {_ps_quote(dst)} -Force",
            f"Переместить «{target}» → «{dst}»",
            "Переместит файл/папку — исходник исчезнет с прежнего места.",
        )

    if a == "make_dir":
        if not target:
            return {"error": "не указан путь"}
        return spec(
            "Создание папки",
            f"New-Item -Path {_ps_quote(target)} -ItemType Directory -Force",
            f"Создать папку «{target}»",
            "Создаст папку (включая промежуточные уровни).",
        )

    # ---------- реестр ----------
    if a in ("registry_read", "reg_read"):
        key = _norm_reg_key(target)
        if not key:
            return {"error": "не указан ключ реестра"}
        if name:
            cmd = f"Get-ItemProperty -Path {_ps_quote(key)} -Name {_ps_quote(name)} -ErrorAction Stop"
        else:
            cmd = f"Get-ItemProperty -Path {_ps_quote(key)} -ErrorAction Stop"
        return spec(
            "Чтение реестра",
            cmd,
            f"Прочитать реестр: {key}" + (f", значение «{name}»" if name else ""),
            "Только чтение, ничего не изменяет.",
        )

    if a in ("registry_write", "reg_write"):
        key = _norm_reg_key(target)
        vtype = str(params.get("value_type") or "String").strip()
        if vtype not in _REG_VALUE_TYPES:
            vtype = "String"
        if not key or not name:
            return {"error": "нужны ключ реестра и имя значения"}
        cmd = (
            f"if (-not (Test-Path {_ps_quote(key)})) {{ New-Item -Path {_ps_quote(key)} -Force | Out-Null }}; "
            f"Set-ItemProperty -Path {_ps_quote(key)} -Name {_ps_quote(name)} -Value {_ps_quote(value)} -Type {vtype} -Force"
        )
        return spec(
            "Запись в реестр",
            cmd,
            f"Записать в реестр: {key}\\{name} = «{value}» (тип {vtype})",
            "Изменит или создаст значение реестра — может повлиять на поведение системы или приложений.",
        )

    if a in ("registry_delete", "reg_delete"):
        key = _norm_reg_key(target)
        if not key or not name:
            return {"error": "нужны ключ реестра и имя значения"}
        return spec(
            "Удаление из реестра",
            f"Remove-ItemProperty -Path {_ps_quote(key)} -Name {_ps_quote(name)} -ErrorAction Stop",
            f"Удалить значение реестра: {key}\\{name}",
            "Удалит только указанное значение; остальные значения ключа сохранятся.",
        )

    # ---------- автозапуск ----------
    if a in ("startup_list", "autostart_list"):
        return spec(
            "Список автозапуска",
            "Write-Output '=== HKCU Run ==='; Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -ErrorAction SilentlyContinue | Format-List | Out-String; "
            "Write-Output '=== Startup folder ==='; Get-ChildItem \"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\" -ErrorAction SilentlyContinue | Select-Object Name | Out-String",
            "Показать записи автозапуска (реестр HKCU Run + папка автозагрузки)",
            "Только чтение, ничего не изменяет.",
        )

    if a in ("startup_add", "autostart_add"):
        command = str(params.get("value") or params.get("command") or "").strip()
        if not target or not command:
            return {"error": "нужны имя записи и команда запуска"}
        return spec(
            "Добавить в автозапуск",
            f"Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -Name {_ps_quote(target)} -Value {_ps_quote(command)} -Force",
            f"Добавить в автозапуск: «{target}» → «{command}»",
            "Программа будет запускаться автоматически при входе пользователя в систему.",
        )

    if a in ("startup_remove", "autostart_remove"):
        if not target:
            return {"error": "не указано имя записи автозапуска"}
        return spec(
            "Убрать из автозапуска",
            f"Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -Name {_ps_quote(target)} -ErrorAction Stop",
            f"Убрать из автозапуска запись «{target}»",
            "Программа больше не будет запускаться при входе в систему.",
        )

    # ---------- процессы ----------
    if a in ("process_list", "ps"):
        if target:
            cmd = f"Get-Process -Name {_ps_quote(target)} -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, CPU, WorkingSet | Format-Table -AutoSize | Out-String -Width 200"
            desc = f"Показать процессы с именем «{target}»"
        else:
            cmd = "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 30 Id, ProcessName, CPU, WorkingSet | Format-Table -AutoSize | Out-String -Width 200"
            desc = "Показать список процессов (топ-30 по памяти)"
        return spec("Список процессов", cmd, desc, "Только чтение, ничего не изменяет.")

    if a in ("process_kill", "kill"):
        if not target:
            return {"error": "не указан PID или имя процесса"}
        if target.isdigit():
            cmd = f"Stop-Process -Id {target} -Force"
            desc = f"Завершить процесс с PID {target}"
        else:
            cmd = f"Stop-Process -Name {_ps_quote(target)} -Force"
            desc = f"Завершить все процессы с именем «{target}»"
        return spec(
            "Завершение процесса",
            cmd,
            desc,
            "Принудительно завершит процесс без сохранения его данных; возможно закрытие несохранённой работы.",
        )

    if a in ("process_start", "start"):
        command = str(params.get("value") or params.get("command") or "").strip()
        if not command:
            return {"error": "не указана команда запуска"}
        return spec(
            "Запуск процесса",
            f"Start-Process -FilePath {_ps_quote(command)}",
            f"Запустить «{command}»",
            "Запустит указанную программу.",
        )

    return {"error": f"неизвестное действие «{action}»"}


def run_tool(action: str, **params) -> str:
    """Точка входа из tool-цикла: подтверждение -> выполнение -> результат."""
    spec = build_action(action, **params)
    if spec.get("error"):
        return f"⚠️ {spec['error']}"

    decision = _request_approval(spec)
    if decision != "approve":
        return f"⛔ Операция отклонена пользователем: {spec['description']}"

    result = _run_ps(spec["command"])
    if result["ok"]:
        out = result["stdout"].strip()
        return out or "✅ Выполнено успешно"
    err = result["stderr"].strip() or result["stdout"].strip() or "неизвестная ошибка"
    return f"❌ Ошибка выполнения (код {result['rc']}): {err}"