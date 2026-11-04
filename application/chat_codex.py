"""Codex chat orchestration, independent of paid API chat and its fallback routes."""

import json
import threading
import uuid

import codex_engine
from codex_proxy import ProxyUnavailable
from config_manager import load_chat_history, save_chat_history


def run_turn(request, request_data, session_id, profile_id, profile, *,
             service, sessions, progress, file_access):
    cancel = threading.Event()
    request._chat_session_id = session_id
    steps = []
    owns_progress = False
    try:
        with progress.lock:
            if session_id in progress.cancel_events:
                raise codex_engine.ProtocolError("В этом диалоге уже выполняется ход")
            progress.cancel_events[session_id] = cancel
            progress.thinking[session_id] = steps
            progress.reasoning[session_id] = ""
            progress.reply[session_id] = ""
            owns_progress = True
        if session_id in sessions.deleted:
            raise codex_engine.ProtocolError("Диалог удалён")
        from codex_attachments import prepare_attachments
        input_items, attachment_history = prepare_attachments(request_data.get("attachments", []), file_access, profile)
        meta = sessions.read_metadata().get(session_id) or {}
        if meta.get("personaId") and meta["personaId"] != profile_id:
            raise codex_engine.ProtocolError("Диалог относится к другому профилю")
        session = service.get(profile_id, profile)
        prompt = str(request_data.get("prompt") or "")
        operation = str(request_data.get("operation_id") or uuid.uuid4())
        result = session.run(session_id, prompt, operation, cancel, steps.append,
                             timeout=None,  # HTTP profile timeouts do not limit Codex turns.
                             on_text=lambda text: progress.reply.__setitem__(session_id, text), input_items=input_items)
        if session_id not in sessions.deleted and not result.get("cancelled"):
            history = load_chat_history(session_id)
            if not any(item.get("codex_operation") == operation and item.get("role") == "assistant" for item in history):
                history.extend([{"role": "user", "content": prompt, "codex_operation": operation, "attachments": attachment_history},
                                *result.get("steers", []), {"role": "assistant", "content": result["reply"],
                               "codex_operation": operation, "reasoning": result.get("reasoning", ""), "thinking": result.get("thinking", []), "activity": result.get("activity", {}), "images": result.get("images", [])}])
            save_chat_history(session_id, history)
            sessions.memory[session_id] = history
        request._set_cors()
        request.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception as error:
        if isinstance(error, (codex_engine.CodexBlocked, ProxyUnavailable)):
            message = str(error) + ": " + error.reason
        elif isinstance(error, codex_engine.ProtocolError):
            message = str(error)
        else:
            message = "Внутренняя ошибка обработки чата. Проверьте сохранённый результат перед повторной отправкой."
        request._set_cors(503)
        request.wfile.write(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    finally:
        with progress.lock:
            if owns_progress and progress.cancel_events.get(session_id) is cancel:
                progress.cancel_events.pop(session_id, None)
                progress.thinking.pop(session_id, None)
                progress.reasoning.pop(session_id, None)
                progress.reply.pop(session_id, None)
    return


def steer_turn(request, *, service, sessions, file_access):
    try:
        data = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))))
        if not isinstance(data, dict):
            raise codex_engine.ProtocolError("Некорректное уточнение")
        session_id = str(data.get("session_id") or "")
        profile_id = str(data.get("persona_id") or "")
        meta = sessions.read_metadata().get(session_id) or {}
        if session_id in sessions.deleted or meta.get("personaId") != profile_id:
            raise codex_engine.ProtocolError("Диалог не принадлежит этому профилю")
        # Do not call service.get: profile edits must not replace a busy runtime.
        with service.lock:
            saved = service.sessions.get(profile_id)
        if not saved:
            raise codex_engine.ProtocolError("В этом диалоге нет активного ответа Codex")
        session = saved[1]
        prompt = str(data.get("prompt") or "").strip()
        if not prompt and not data.get("attachments"):
            raise codex_engine.ProtocolError("Введите уточнение")
        from codex_attachments import prepare_attachments
        inputs, history = prepare_attachments(data.get("attachments", []), file_access, session.profile)
        session.steer(session_id, str(data.get("operation_id") or ""), prompt, inputs, history)
        request._set_cors()
        request.wfile.write(b'{"ok":true}')
    except Exception as error:
        message = str(error) if isinstance(error, codex_engine.ProtocolError) else codex_engine.PROXY_BLOCKED
        request._set_cors(409)
        request.wfile.write(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
