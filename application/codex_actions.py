"""Codex host actions and local integration endpoints; protection stays in runtime."""

import json
import threading
import uuid
import urllib.parse
from datetime import datetime
from pathlib import Path

import codex_engine
from tool_execution import execute_tool, ToolExecutionError
from config_manager import load_chat_history, save_chat_history


def execute_host_tool(name, arguments, profile_id, cancel, *, service, workspaces,
                      load_config, save_config, read_notes, prepend_note):
    # These handlers use only stdlib/local persistence. Anything involving an
    # HTTP/ML/document dependency runs in the separate network-less tool worker.
    def save_note(title, content):
        text = str(content or "").strip()[:4000]
        if not text:
            raise ValueError("Пустая заметка")
        note = {"id": "note_" + uuid.uuid4().hex, "title": str(title or "Заметка от ассистента")[:120],
                "content": text, "createdAt": datetime.now().strftime("%d.%m.%Y, %H:%M:%S"), "author": "assistant"}
        prepend_note(note)
        return {"saved": True, "note": note}
    def set_prompt(prompt):
        cfg = load_config()
        if profile_id not in cfg.get("profiles", {}):
            raise ValueError("Профиль не найден")
        cfg["profiles"][profile_id]["system_prompt"] = str(prompt)[:50000]
        save_config(cfg)
        return "Системный промпт этого профиля сохранён"
    def host_permission(method, command, description, automatic=False):
        if cancel.is_set():
            raise ValueError("Действие отменено")
        if not automatic:
            decision = service.approvals.wait(profile_id, action_session_id, method,
                {"command":command,"reason":description},cancel,lambda text: str(text))
            if decision.get("decision") != "accept":
                raise ValueError("Действие Windows отменено")
        if cancel.is_set():
            raise ValueError("Действие отменено")
    # Thread/turn is located on the host; the model cannot choose another session.
    saved=service.sessions.get(profile_id)
    active=None
    if saved:
        with saved[1].lock:
            active=next((item for item in saved[1].active.values() if item.get("cancel") is cancel),None)
    action_session_id=active["session_id"] if active else ""
    def windows_command(command, shell="powershell", cwd=None):
        from code_workspace import windows_diagnostic
        native = bool(saved and getattr(saved[1].runtime, "native", False))
        if not native and command not in ("ipconfig","ipconfig /all"):
            raise ValueError("Произвольные команды Windows заблокированы до настройки сетевой изоляции ОС")
        profile=(load_config().get("profiles") or {}).get(profile_id,{})
        host_permission("item/commandExecution/requestApproval",command,"Выполнение команды на вашем компьютере Windows",
                        automatic=codex_engine.settings(profile).get("auto_approve_commands") is True)
        if native:
            from native_codex.commands import run
            from code_workspace import windows_desktop
            folder = cwd or saved[1].runtime.workspace or windows_desktop()
            return run(command, shell, folder, cancel)
        return windows_diagnostic(command)
    def windows_write_file(file_name,content,location):
        from code_workspace import create_windows_file, windows_desktop
        if location == "desktop":
            root=windows_desktop()
        elif location == "project":
            root=workspaces.root(profile_id)
        else:
            raise ValueError("Выберите desktop или project")
        host_permission("item/fileChange/requestApproval",str(Path(root)/file_name),"Создание нового файла на вашем компьютере Windows")
        return create_windows_file(root,file_name,content)
    def telegram_send_file(path):
        sender = getattr(service, "telegram_export", None)
        if sender is None:
            raise ValueError("Telegram не подключён")
        try:
            return sender(profile_id, action_session_id, path, cancel)
        except codex_engine.ProtocolError as error:
            raise ToolExecutionError(str(error)) from None
        except OSError:
            raise ToolExecutionError("Файл недоступен для отправки в Telegram") from None
    def windows_open_app(app):
        if not saved or not getattr(saved[1].runtime, "native", False):
            raise ValueError("Запуск окна доступен только в настроенном native Windows runtime")
        profile = (load_config().get("profiles") or {}).get(profile_id, {})
        host_permission("item/commandExecution/requestApproval", "Открыть видимое окно: " + str(app),
                        "Запуск отдельного окна терминала на вашем компьютере",
                        automatic=codex_engine.settings(profile).get("auto_approve_commands") is True)
        from native_codex.desktop import open_app
        try:
            return open_app(app, cancel)
        except OSError:
            raise ToolExecutionError("Windows не разрешила запустить приложение или проверить его окно. Открытие не подтверждено.") from None
    def archive(name, arguments):
        handler = getattr(service, "telegram_archive", None)
        if handler is None:
            raise ValueError("Архив Telegram не подключён")
        try:
            return handler(profile_id, action_session_id, cancel, name, arguments)
        except (codex_engine.ProtocolError, OSError) as error:
            raise ToolExecutionError(str(error) if isinstance(error, codex_engine.ProtocolError) else "Файл архива недоступен") from None
    def telegram_find_files(name="", date_from=None, date_to=None, limit=10):
        return archive("telegram_find_files", {"name":name, "date_from":date_from, "date_to":date_to, "limit":limit})
    def telegram_read_file(archive_id, start_line=1, line_count=40):
        return archive("telegram_read_file", {"archive_id":archive_id, "start_line":start_line, "line_count":line_count})
    def install_skill(action, source, name="", description="", fingerprint=""):
        if not saved or not getattr(saved[1].runtime, "native", False):
            raise ValueError("Установка скиллов доступна в нативном профиле Codex")
        from services.skill_imports import install_skill as import_skill
        return import_skill(service.directory, load_config, save_config, action=action, source=source,
                            name=name, description=description, fingerprint=fingerprint, cancel=cancel)
    host_tools = ("windows_command", "windows_write_file", "telegram_send_file", "windows_open_app", "telegram_find_files", "telegram_read_file", "install_skill")
    if name in host_tools and active is None:
        raise ValueError("Нет активного хода для действия Windows")
    try:
        return execute_tool(name, arguments, {"save_note": save_note, "list_notes": read_notes, "set_system_prompt": set_prompt,
                                             "windows_command":windows_command,"windows_write_file":windows_write_file,
                                             "telegram_send_file":telegram_send_file,"windows_open_app":windows_open_app,
                                             "telegram_find_files":telegram_find_files,"telegram_read_file":telegram_read_file,
                                             "install_skill":install_skill}, cancel)
    except ValueError as error:
        if name in host_tools:
            raise ToolExecutionError(str(error)) from None
        raise


def code_routes(request, code_path, load_config, service, workspaces):
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query)
    profile_id = params.get("profile_id", [""])[0]
    try:
        if profile_id not in (load_config().get("profiles") or {}):
            raise ValueError("Профиль не найден")
        if code_path == "/api/code/events":
            sid = params.get("session_id", [""])[0]
            saved = service.sessions.get(profile_id)
            payload = {"events": list(saved[1].events.get(sid, [])) if saved else []}
        elif code_path == "/api/code/files":
            payload = workspaces.files(profile_id)
        else:
            relative = params.get("path", [""])[0]
            if code_path.endswith("preview"):
                payload = workspaces.preview(profile_id, relative)
            else:
                profile = (load_config().get("profiles") or {})[profile_id]
                payload = workspaces.diff(profile_id, relative,
                    lambda path: service.project_diff(profile_id, profile, path))
        request._set_cors()
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error) if isinstance(error, (codex_engine.CodexBlocked, codex_engine.ProtocolError)) else "Просмотр недоступен. Выберите локальную рабочую папку и доступный файл."
        payload = {"error": message}
        request._set_cors(400)
    request.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return


def limits(request, load_config, service):
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query)
    profile_id = params.get("profile_id", [""])[0]
    profile = (load_config().get("profiles") or {}).get(profile_id)
    if not isinstance(profile, dict) or codex_engine.engine(profile) != "codex":
        request._set_cors(404)
        request.wfile.write(b'{"error":"Codex profile not found"}')
        return
    # Только локальный кэш: частое чтение не запускает Docker,
    # авторизацию, переподключение или внешние запросы.
    saved = service.sessions.get(profile_id)
    session = saved[1] if saved else None
    live = bool(session and session.connection is not None and not session.connection.closed
                and not session.closed.is_set() and not session.runtime.stopped.is_set()
                and not session.limits_error and profile_id not in service.errors)
    payload = {"limits": service.limits.read(profile_id, live=live)}
    request._set_cors()
    request.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return


def status(request, load_config, service):
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query)
    profile_id = params.get("profile_id", [""])[0]
    profile = (load_config().get("profiles") or {}).get(profile_id)
    if not isinstance(profile, dict):
        request._set_cors(404)
        request.wfile.write(b'{"error":"Profile not found"}')
        return
    request._set_cors()
    request.wfile.write(json.dumps(service.status(profile_id, profile), ensure_ascii=False).encode("utf-8"))
    return


def pending_actions(request, service):
    import win_tools

    request._set_cors()
    request.wfile.write(
        json.dumps({"pending": win_tools.list_pending() + service.approvals.pending()}, ensure_ascii=False).encode("utf-8")
    )
    return


def recover(request, load_config, service, sessions):
    req = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))).decode("utf-8"))
    pid, sid = str(req.get("profile_id") or ""), str(req.get("session_id") or "")
    profile = (load_config().get("profiles") or {}).get(pid)
    try:
        if not isinstance(profile, dict) or not sid or sid in sessions.deleted:
            raise codex_engine.ProtocolError("Диалог не найден")
        meta = sessions.read_metadata().get(sid) or {}
        if meta.get("personaId") and meta["personaId"] != pid:
            raise codex_engine.ProtocolError("Диалог относится к другому профилю")
        result = service.get(pid, profile).recover(sid)
        history = load_chat_history(sid)
        operation = result.get("operation_id")
        if not result.get("cancelled") and not any(item.get("codex_operation") == operation and item.get("role") == "assistant" for item in history):
            history.extend([{"role": "user", "content": result.get("prompt", ""), "codex_operation": operation},
                            {"role": "assistant", "content": result.get("reply", ""), "codex_operation": operation,
                             "thinking":result.get("thinking", []), "activity":result.get("activity", {}), "images":result.get("images", [])}])
            save_chat_history(sid, history)
            sessions.memory[sid] = history
        request._set_cors()
    except Exception as error:
        result = {"error": str(error) if isinstance(error, (codex_engine.CodexBlocked, codex_engine.ProtocolError)) else codex_engine.PROXY_BLOCKED}
        request._set_cors(503)
    request.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return


def select_workspace(request, load_config, save_config, workspaces, pick_directory, desktop_dir):
    req = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))).decode("utf-8"))
    profile_id = str(req.get("profile_id") or "")
    cfg = load_config()
    profile = (cfg.get("profiles") or {}).get(profile_id)
    if not isinstance(profile, dict):
        request._set_cors(404)
        request.wfile.write(b'{"error":"Profile not found"}')
        return
    try:
        if request.path.endswith("select"):
            chosen = pick_directory(desktop_dir())
            if chosen:
                workspace = workspaces.grant(profile_id, chosen)
                profile["codex"] = {**codex_engine.settings(profile), "workspace": workspace}
                save_config(cfg)
            payload = {"path": chosen or ""}
        else:
            workspaces.open_vscode(profile_id)
            payload = {"ok": True}
        request._set_cors()
    except (OSError, ValueError):
        payload = {"error": "Не удалось открыть локальную рабочую папку или VS Code"}
        request._set_cors(400)
    request.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return


def account_action(request, load_config, service):
    req = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))).decode("utf-8"))
    profile_id = str(req.get("profile_id") or "")
    profile = (load_config().get("profiles") or {}).get(profile_id)
    if not isinstance(profile, dict):
        request._set_cors(404)
        request.wfile.write(b'{"error":"Profile not found"}')
        return
    result = service.action(profile_id, profile, request.path.rsplit("/", 1)[1])
    request._set_cors(200 if result["available"] else 503)
    request.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return


def decide_action(request, service):
    import win_tools

    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8", errors="replace")
    try:
        req_data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_data = {}
    action_id = str((req_data or {}).get("id") or "").strip()
    if action_id.startswith("codex_"):
        ok = service.approvals.decide(action_id, request.path.endswith("/approve"))
    elif request.path.endswith("/approve"):
        ok = win_tools.approve(action_id) if action_id else False
    else:
        ok = win_tools.reject(action_id) if action_id else False
    request._set_cors()
    request.wfile.write(json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8"))
    return


