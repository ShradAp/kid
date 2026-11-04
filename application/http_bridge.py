"""Local HTTP bridge: authorization, request limits and scenario dispatch.

The factory binds concrete components and callbacks; it never imports the desktop
composition root. Route order and response contracts match the original bridge.
"""

import http.server
import json
import os
import urllib.parse

import api_client
import codex_engine
from bridge_security import authorized, request_length
from application import codex_actions, chat_codex, file_transfer, settings_profiles
from application.studio_profiles import STUDIO_ACTIVE_KEY
from services import voice_input


def make_bridge_handler(*, get_bridge_port, file_access, user_data_dir, backup_dir,
                        sessions, studio_sessions, documents, providers, codex,
                        workspaces, progress, log_buffer, load_config, save_config,
                        read_history, read_notes, write_notes, get_balance,
                        create_backup, connect_startup_codex, pick_directory,
                        get_desktop_dir, get_generated_file, get_uploaded_document,
                        run_api_chat, log_event, telegram=None):
    class BridgeHandler(http.server.BaseHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def _authorized(self):
            if authorized(self.headers, get_bridge_port()):
                return True
            self._set_cors(403)
            self.wfile.write(b'{"error":"Forbidden"}')
            self.close_connection = True
            return False

        def _set_cors(self, status=200):
            self._headers_sent = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # Клиент мог закрыть вкладку или отменить запрос после получения ответа.
                # Это штатное завершение соединения, а не ошибка обработки чата.
                return

        def do_OPTIONS(self):
            self._set_cors(403)

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == '/api/updates':
                from services.updates import handle
                handle(self, 'GET')
                return
            if urllib.parse.urlsplit(self.path).path == "/api/extensions":
                from application.extension_actions import handle
                handle(self, "GET", root=user_data_dir, load_config=load_config, save_config=save_config, service=codex, sessions=sessions)
                return
            if self.path == "/api/telegram/settings" and telegram is not None:
                from application.telegram_actions import handle
                handle(self, telegram, "GET")
                return
            if self.path == "/api/notes":
                self._set_cors()
                self.wfile.write(json.dumps({"notes": read_notes()}, ensure_ascii=False).encode("utf-8"))
                return
            code_path = urllib.parse.urlsplit(self.path).path
            if code_path == "/api/voice-input":
                voice_input.handle_get(self)
                return
            if code_path in ("/api/code/files", "/api/code/preview", "/api/code/diff", "/api/code/events"):
                codex_actions.code_routes(self, code_path, load_config, codex, workspaces)
                return
            if urllib.parse.urlsplit(self.path).path == "/api/codex/limits":
                codex_actions.limits(self, load_config, codex)
                return
            if urllib.parse.urlsplit(self.path).path == "/api/codex/status":
                codex_actions.status(self, load_config, codex)
                return
            if self.path.startswith("/api/download-local"):
                file_transfer.download_local(self, file_access)
                return

            if self.path.startswith("/api/codex-images/"):
                file_transfer.codex_image(self, user_data_dir)
                return

            if self.path.startswith("/api/download/"):
                file_transfer.download_upload(self)
                return

            if self.path.startswith("/api/generated/") and self.path.endswith("/preview"):
                file_transfer.generated_preview(self)
                return

            if self.path.startswith("/api/generated/"):
                file_transfer.download_generated(self, get_generated_file)
                return

            if self.path == "/api/word/presets":
                self._set_cors()
                self.wfile.write(
                    json.dumps(documents.presets(), ensure_ascii=False).encode("utf-8")
                )
                return

            if self.path == "/api/document-studio-settings":
                settings_profiles.get_studio_settings(self, load_config)
                return

            if self.path == "/api/studio/init":
                self._set_cors()
                self.wfile.write(
                    json.dumps(studio_sessions.init_payload(), ensure_ascii=False).encode("utf-8")
                )
                return

            if self.path == "/api/init":
                settings_profiles.initial_state(self, sessions, read_history, get_balance, read_notes, load_config)
                return
            elif self.path.startswith("/api/models"):
                cfg = load_config()
                profiles = cfg.get("profiles", {})
                active_profile_key = cfg.get("active_profile", "main")
                active_data = profiles.get(active_profile_key, {})
                if codex_engine.engine(active_data) == "codex":
                    result = codex.status(active_profile_key, active_data)
                    self._set_cors(200 if result["available"] else 503)
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    return

                self._set_cors()
                self.wfile.write(json.dumps(providers.models(active_data), ensure_ascii=False).encode("utf-8"))
            elif self.path == "/api/prompt-generator-settings":
                settings_profiles.get_prompt_generator_settings(self, load_config)
                return
            elif self.path == "/api/os-tools/pending":
                codex_actions.pending_actions(self, codex)
                return
            elif self.path == "/api/proxy-settings":
                # Настройки прокси (вкладка «Прокси» в настройках интерфейса).
                settings_profiles.get_proxy_settings(self, load_config)
                return
            elif self.path == "/api/file-click-mode":
                settings_profiles.get_file_click_mode(self, load_config)
                return
            elif self.path == "/api/tool-send-mode":
                settings_profiles.get_tool_send_mode(self, load_config)
                return
            elif self.path == "/api/download-base-path":
                settings_profiles.get_download_base_path(self, load_config)
                return
            elif self.path == "/api/balance/refresh":
                cfg = load_config()
                profiles = cfg.get("profiles") or {}
                active_key = cfg.get("active_profile", "main")
                active_data = profiles.get(active_key, {}) or {}
                if codex_engine.engine(active_data) == "codex":
                    result = codex.action(active_key, active_data, "refresh")
                    self._set_cors(200 if result["available"] else 503)
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    return
                balance = get_balance(
                    active_data.get("api_key", ""),
                    active_data.get("base_url", ""),
                    force=True,
                )
                self._set_cors()
                self.wfile.write(
                    json.dumps({"balance": balance}, ensure_ascii=False).encode("utf-8")
                )
            elif self.path.startswith("/api/logs"):
                self._set_cors()
                self.wfile.write(
                    json.dumps({"logs": list(log_buffer)}, ensure_ascii=False).encode("utf-8")
                )
            elif self.path.startswith("/api/progress"):
                # Живой прогресс генерации: фронтенд опрашивает во время /api/chat,
                # чтобы плашка Thinking process показывала инструменты в реальном времени.
                progress.write_snapshot(self)
                return
            elif self.path == "/api/backups":
                backup_dir = str(backup_dir)
                try:
                    os.makedirs(backup_dir, exist_ok=True)
                    backups = [
                        name for name in os.listdir(backup_dir)
                        if name.startswith("backup_") and name.endswith(".zip")
                    ]
                    backups.sort(reverse=True)
                    self._set_cors()
                    self.wfile.write(json.dumps({"count": len(backups), "backups": backups}, ensure_ascii=False).encode("utf-8"))
                except OSError as exc:
                    self._set_cors(500)
                    self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            else:
                self._set_cors(404)

        def do_POST(self):
            if not self._authorized():
                return
            try:
                request_length(self.headers, upload=self.path == "/api/upload")
            except (ValueError, OverflowError) as exc:
                self._set_cors(413 if isinstance(exc, OverflowError) else 400)
                self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
                self.close_connection = True
                return
            try:
                self._do_POST()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as exc:
                # Клиент (например, WebView) мог закрыть запрос после получения ответа.
                # Это штатная ситуация при отмене/перезапуске запроса, а не ошибка сервера.
                log_event(f"[*] Клиент закрыл соединение до отправки ответа: {exc}")
                # Живой прогресс не должен «залипать» в active=true после сброса клиента.
                progress.thinking.pop(getattr(self, "_chat_session_id", None), None)
                progress.reasoning.pop(getattr(self, "_chat_session_id", None), None)
                stopped = progress.cancel_events.pop(getattr(self, "_chat_session_id", None), None)
                if stopped is not None:
                    stopped.set()
            except Exception as exc:
                # Чистим живой прогресс: иначе /api/progress навсегда оставался
                # бы активным для этой сессии после аварийного завершения запроса.
                progress.thinking.pop(getattr(self, "_chat_session_id", None), None)
                progress.reasoning.pop(getattr(self, "_chat_session_id", None), None)
                progress.cancel_events.pop(getattr(self, "_chat_session_id", None), None)
                # Без этого блока любая необработанная ошибка обрывала соединение
                # вообще без тела ответа — фронтенд не получал финальный ответ ассистента
                # и просто терял его. Теперь клиент всегда получает JSON с ошибкой.
                log_event(f"[!] Ошибка обработки запроса {self.path}: {exc}")
                try:
                    if not getattr(self, "_headers_sent", False):
                        self._set_cors(500)
                        self.wfile.write(
                            json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                        )
                except Exception:
                    pass

        def _do_POST(self):
            if self.path == '/api/updates/install':
                from services.updates import handle
                handle(self, 'POST')
                return
            if self.path == "/api/extensions":
                from application.extension_actions import handle
                handle(self, "POST", root=user_data_dir, load_config=load_config, save_config=save_config, service=codex, sessions=sessions)
                return
            if self.path in ("/api/telegram/settings", "/api/telegram/clear", "/api/telegram/stop", "/api/telegram/test-file", "/api/telegram/panel", "/api/telegram/archive/clear", "/api/telegram/test-integration") and telegram is not None:
                from application.telegram_actions import handle
                handle(self, telegram, "POST")
                return
            if self.path == "/api/reveal-local":
                file_transfer.reveal_local(self)
                return
            if self.path == "/api/voice-input":
                voice_input.handle_post(self)
                return
            if self.path == "/api/settings/reveal-secret":
                settings_profiles.reveal_secret(self, load_config)
                return
            if self.path == "/api/codex/recover":
                codex_actions.recover(self, load_config, codex, sessions)
                return
            if self.path in ("/api/code/select", "/api/code/open-vscode"):
                codex_actions.select_workspace(self, load_config, save_config, workspaces, pick_directory, get_desktop_dir)
                return
            if self.path in ("/api/codex/login", "/api/codex/logout", "/api/codex/refresh", "/api/codex/retry"):
                codex_actions.account_action(self, load_config, codex)
                return
            if self.path.startswith("/api/generated/") and self.path.endswith("/open"):
                file_transfer.open_generated(self, get_generated_file)
                return

            if self.path == "/api/stop":
                # Кнопка «стоп» во фронтенде: выставляем событие отмены для сессии.
                # Цикл генерации /api/chat проверяет его между шагами, а долгий вызов
                # LLM перестаёт ожидаться (см. _call_chat_cancellable).
                progress.stop(self, log_event)
                return

            if self.path in ("/api/os-tools/approve", "/api/os-tools/reject"):
                codex_actions.decide_action(self, codex)
                return

            if self.path == "/api/backups":
                try:
                    filename, count = create_backup()
                    self._set_cors()
                    self.wfile.write(json.dumps({"ok": True, "filename": filename, "count": count}, ensure_ascii=False).encode("utf-8"))
                except Exception as exc:
                    log_event(f"[!] Ошибка создания бэкапа: {exc}")
                    self._set_cors(500)
                    self.wfile.write(json.dumps({"ok": False, "error": "Не удалось создать бэкап: %s" % exc}, ensure_ascii=False).encode("utf-8"))
                return

            if self.path == "/api/word/presets":
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                try:
                    req_data = json.loads(body) if body else {}
                    if not isinstance(req_data, dict):
                        raise ValueError("Некорректный запрос сохранения пресета")
                    payload = documents.save_preset(req_data)
                    self._set_cors()
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                    log_event(f"[!] /api/word/presets: {exc}")
                    self._set_cors(400)
                    self.wfile.write(
                        json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    )
                return

            if self.path == "/api/document-studio-settings":
                settings_profiles.save_studio_settings(self, load_config, save_config)
                return

            if self.path.startswith("/api/studio/"):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                try:
                    req_data = json.loads(body) if body else {}
                    if not isinstance(req_data, dict):
                        raise ValueError("Некорректный запрос студии")
                    payload = studio_sessions.handle(self.path, req_data)
                    self._set_cors()
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                    log_event(f"[!] {self.path}: {exc}")
                    self._set_cors(400)
                    self.wfile.write(
                        json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    )
                return

            if self.path == "/api/proxy-settings":
                settings_profiles.save_proxy_settings(self, load_config, save_config, codex, connect_startup_codex, log_event)
                return

            if self.path == "/api/file-click-mode":
                settings_profiles.save_file_click_mode(self, load_config, save_config)
                return

            if self.path == "/api/tool-send-mode":
                settings_profiles.save_tool_send_mode(self, load_config, save_config)
                return

            if self.path == "/api/download-base-path":
                settings_profiles.save_download_base_path(self, load_config, save_config)
                return

            if self.path == "/api/search-settings":
                settings_profiles.save_search_settings(self, load_config, save_config)
                return

            if self.path == "/api/prompt-generator-settings":
                settings_profiles.save_prompt_generator_settings(self, load_config, save_config)
                return

            if self.path == "/api/upload":
                file_transfer.upload(self)
                return

            if self.path == "/api/save-file":
                # Нативное «Сохранить как»: в WebView2 скриптовое скачивание blob-ссылки
                # может молча игнорироваться, поэтому файл сохраняет сам бэкенд.
                file_transfer.save_file(self, get_uploaded_document, get_generated_file, load_config, log_event)
                return

            if self.path == "/api/pick-files":
                file_transfer.pick_files(self, file_access)
                return

            if self.path == "/api/pick-directory":
                file_transfer.pick_directory(self, pick_directory, log_event)
                return

            if self.path == "/api/prompt-generator-settings":
                settings_profiles.read_prompt_generator_settings_legacy(self, load_config)
                return

            if self.path == "/api/prompt-generator":
                settings_profiles.generate_prompt(self, load_config, save_config, api_client.call_chat_api)
                return

            if self.path == "/api/chat/steer":
                chat_codex.steer_turn(self, service=codex, sessions=sessions, file_access=file_access)
                return
            if self.path == "/api/chat":
                content_length = int(self.headers.get("Content-Length", 0))
                # JSON чата содержит только текст и ID временных файлов.
                max_request_bytes = 2 * 1024 * 1024
                if content_length > max_request_bytes:
                    self.send_response(413)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Размер запроса превышает 512 МБ"}, ensure_ascii=False).encode("utf-8"))
                    return
                body = self.rfile.read(content_length).decode("utf-8")
                try:
                    req_data = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": f"Некорректный JSON: {exc}"}, ensure_ascii=False).encode("utf-8"))
                    return

                session_id = req_data.get("session_id", "default")
                session_id = str(session_id or "default")
                # Route before attachment extraction, memory, research, balance or
                # ANY API work. Retained API settings never act as Codex fallback.
                routing_cfg = load_config()
                routing_id = str(req_data.get("persona_id") or routing_cfg.get("active_profile", "main"))
                if req_data.get("studio"):
                    routing_id = str(req_data.get("studio_persona_id") or routing_cfg.get(STUDIO_ACTIVE_KEY) or routing_id)
                routing_profile = (routing_cfg.get("profiles") or {}).get(routing_id)
                if not isinstance(routing_profile, dict):
                    self._set_cors(404)
                    self.wfile.write(b'{"error":"Profile not found"}')
                    return
                if codex_engine.engine(routing_profile) == "codex":
                    chat_codex.run_turn(
                        self, req_data, session_id, routing_id, routing_profile,
                        service=codex, sessions=sessions,
                        progress=progress, file_access=file_access,
                    )
                    return
                # Document Studio идёт по собственным профилям и собственному хранилищу
                # чатов — режим определяется здесь, до чтения меты и истории.
                run_api_chat(self, req_data, session_id)
            elif self.path == "/api/session/activate":
                # Запоминает последний открытый чат — /api/init вернёт его после рестарта.
                sessions.activate(self)
            elif self.path == "/api/notes":
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                try:
                    req_data = json.loads(body)
                    notes = req_data.get("notes")
                    if not isinstance(notes, list):
                        raise ValueError("notes must be an array")
                    write_notes(notes)
                    self._set_cors()
                    self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
                except Exception as e:
                    self._set_cors(400)
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            elif self.path == "/api/session/upsert":

                sessions.upsert(self)
            elif self.path == "/api/session/delete":
                sessions.delete(self)
            elif self.path == "/api/session/clear":
                sessions.clear(self)
            elif self.path == "/api/save_personas":
                settings_profiles.save_persona(self, load_config, save_config, codex)
                return

            elif self.path == "/api/delete_persona":
                settings_profiles.delete_persona(self, load_config, save_config)
                return

            elif self.path == "/api/set_active_profile":
                settings_profiles.set_active_profile(self, load_config, save_config, codex, connect_startup_codex)
                return

            elif self.path == "/api/word/compile":
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                try:
                    req_data = json.loads(body)
                    if not isinstance(req_data, dict):
                        raise ValueError("Некорректный запрос компиляции")
                    payload = documents.compile(req_data)
                    self._set_cors()
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                    self._set_cors(400)
                    self.wfile.write(
                        json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    )

            elif self.path in (
                "/api/word/analyze",
                "/api/word/preview",
                "/api/word/apply",
                "/api/word/preset-save",
                "/api/word/state",
                "/api/word/close",
            ):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                handler = {
                    "/api/word/analyze": documents.analyze,
                    "/api/word/preview": documents.preview,
                    "/api/word/apply": documents.apply,
                    # Явное извлечение пресета (автосоздание при анализе отключено),
                    # восстановление состояния после перехода между разделами
                    # и явное закрытие документа.
                    "/api/word/preset-save": documents.save_document_preset,
                    "/api/word/state": documents.document_state,
                    "/api/word/close": documents.close,
                }[self.path]
                try:
                    req_data = json.loads(body) if body else {}
                    if not isinstance(req_data, dict):
                        raise ValueError("Некорректный запрос")
                    payload = handler(req_data)
                    self._set_cors()
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
                    log_event(f"[!] {self.path}: {exc}")
                    self._set_cors(400)
                    self.wfile.write(
                        json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    )

            else:
                self._set_cors(404)

    return BridgeHandler
