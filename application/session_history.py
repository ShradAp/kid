"""Main chat session cache, metadata and lifecycle backed by config_manager."""

import json
import os
from datetime import datetime
from config_manager import load_chat_history, save_chat_history, purge_session_storage


def _meta_title_from_prompt(prompt, limit=48):
    try:
        text = str(prompt or "").strip().replace("\n", " ")
        while "  " in text:
            text = text.replace("  ", " ")
        # срезаем служебный префикс про файлы
        if text.startswith("[Содержимое файла:"):
            # берём последнюю непустую строку как реальный вопрос
            lines = [l.strip() for l in str(prompt or "").splitlines() if l.strip()]
            text = lines[-1] if lines else text
        if len(text) > limit:
            return text[:limit].rstrip() + "…"
        return text or "Новый чат"
    except Exception:
        return "Новый чат"


def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" and p.get("text"):
                    parts.append(str(p["text"]))
                elif p.get("type") == "image_url":
                    parts.append("[изображение]")
        return "\n".join(parts)
    return str(content or "")


class SessionHistory:
    def __init__(self, load_config, save_config, log, forget):
        self.load_config = load_config
        self.save_config = save_config
        self.log = log
        self.forget = forget
        self.memory = self.load_all()
        self.metadata = {}
        self.deleted = set()

    def load_all(self):
        """Загрузить все чаты из отдельных файлов в history/ для совместимости API."""
        try:
            config = self.load_config()
            session_ids = set(config.get("sessions_meta", {}).keys())
            history_dir = os.path.join(os.path.dirname(str(__import__("config_manager").CONFIG_PATH)), "history")
            if os.path.isdir(history_dir):
                session_ids.update(
                    os.path.splitext(name)[0]
                    for name in os.listdir(history_dir)
                    if name.endswith(".json") and not name.endswith(".tmp")
                )
            return {session_id: load_chat_history(session_id) for session_id in session_ids}
        except Exception as e:
            self.log(f"Ошибка чтения истории: {e}")
            return {}


    def save_all(self, history_data):
        """Сохранить каждый чат отдельным JSON-файлом, не помещая историю в config.json."""
        try:
            if not isinstance(history_data, dict):
                history_data = {}
            for session_id, messages in history_data.items():
                save_chat_history(session_id, messages)
        except Exception as e:
            self.log(f"Ошибка сохранения истории: {e}")


    def read_metadata(self):
        try:
            config = self.load_config()
            metadata = config.get("sessions_meta", {})
            return metadata if isinstance(metadata, dict) else {}
        except Exception as e:
            self.log(f"[!] Ошибка чтения метаданных чатов: {e}")
            return {}


    def write_metadata(self, meta):
        try:
            config = self.load_config()
            config["sessions_meta"] = meta if isinstance(meta, dict) else {}
            self.save_config(config)
        except Exception as e:
            self.log(f"[!] Ошибка записи метаданных чатов: {e}")


    def activate(self, request):
        content_length = int(request.headers.get("Content-Length", 0))
        body = request.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
            if not sid:
                request._set_cors(400)
                request.wfile.write(json.dumps({"error": "session_id required"}).encode("utf-8"))
                return
            if sid in self.deleted:
                request._set_cors()
                request.wfile.write(json.dumps({"ok": True, "deleted": True}).encode("utf-8"))
                return
            cfg = self.load_config()
            cfg["last_active_session"] = sid
            self.save_config(cfg)
            request._set_cors()
            request.wfile.write(json.dumps({"ok": True, "session_id": sid}).encode("utf-8"))
        except Exception as e:
            request._set_cors(500)
            request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


    def upsert(self, request):
        content_length = int(request.headers.get("Content-Length", 0))
        body = request.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            sid = str(req_data.get("id") or req_data.get("session_id") or "").strip()
            if not sid:
                request._set_cors(400)
                request.wfile.write(json.dumps({"error": "id required"}).encode("utf-8"))
                return
            if sid in self.deleted:
                # Чат удалён в этом запуске: устаревший клиент не должен
                # воскресить его повторным upsert.
                request._set_cors()
                request.wfile.write(json.dumps({"ok": True, "deleted": True}).encode("utf-8"))
                return
            try:
                _meta = self.read_metadata()
            except Exception:
                _meta = {}
            if not isinstance(_meta, dict):
                _meta = {}
            cur = _meta.get(sid) or {}
            pid = str(req_data.get("personaId") or req_data.get("persona_id") or cur.get("personaId") or "").strip()
            profiles_now = self.load_config().get("profiles", {})
            if not pid:
                pid = self.load_config().get("active_profile", "main")
            if pid not in profiles_now:
                # Чат не может быть привязан к несуществующему профилю:
                # раньше сюда уезжали демо-id фронтенда ("master" и т.п.),
                # и чат «исчезал» из сайдбара после перезапуска.
                self.log(f"[UPSERT] Чат '{sid}': профиль '{pid}' не существует, привязываю к активному")
                pid = self.load_config().get("active_profile", "main")
            title = (req_data.get("title") or cur.get("title") or "Новый чат").strip() or "Новый чат"
            _meta[sid] = {
                "personaId": pid,
                "title": title,
                "timestamp": req_data.get("timestamp") or cur.get("timestamp") or datetime.now().strftime("%d.%m %H:%M"),
            }
            self.metadata[sid] = _meta[sid]
            self.write_metadata(_meta)
            # гарантируем, что сессия есть и в памяти сообщений
            if sid not in self.memory:
                hd = self.load_all()
                self.memory[sid] = hd.get(sid, [])
                self.save_all(self.memory)
            request._set_cors()
            request.wfile.write(json.dumps({"ok": True, "session": {"id": sid, **_meta[sid]}}).encode("utf-8"))
        except Exception as e:
            request._set_cors(500)
            request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


    def delete(self, request):
        content_length = int(request.headers.get("Content-Length", 0))
        body = request.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
            if not sid:
                request._set_cors(400)
                request.wfile.write(json.dumps({"error": "session_id required"}).encode("utf-8"))
                return
            # 1) метка удаления: блокирует поздние записи и «воскрешение»
            #    чата при сохранении других сессий или позднем /api/chat.
            self.deleted.add(sid)
            self.forget(sid)
            # удалённый чат не должен остаться «последним открытым» —
            # иначе после рестарта приложение откроет несуществующий чат
            try:
                cfg = self.load_config()
                if cfg.get("last_active_session") == sid:
                    cfg["last_active_session"] = ""
                    self.save_config(cfg)
            except Exception:
                pass
            # 2) кэш в оперативной памяти сервера
            self.memory.pop(sid, None)
            self.metadata.pop(sid, None)
            # 3) ФИЗИЧЕСКОЕ удаление с диска: файл истории чата, легаси-каталог,
            #    общий sessions_history.json и легаси sessions_meta.json.
            try:
                for item in purge_session_storage(sid):
                    self.log(f"[DELETE] Стёрто: {item}")
            except Exception as purge_error:
                self.log(f"[!] Не удалось удалить файлы чата '{sid}': {purge_error}")
            # 4) мета сессии в конфиге
            try:
                _meta = self.read_metadata()
            except Exception:
                _meta = {}
            if isinstance(_meta, dict) and sid in _meta:
                del _meta[sid]
                self.write_metadata(_meta)
            # 5) пересобираем памятную историю без удалённого чата
            try:
                self.save_all(self.memory)
            except Exception as save_error:
                self.log(f"[!] Не удалось пересохранить историю: {save_error}")
            self.log(f"[DELETE] Чат '{sid}' удалён намертво (память + диск + мета)")
            request._set_cors()
            request.wfile.write(json.dumps({"ok": True, "session_id": sid}).encode("utf-8"))
        except Exception as e:
            request._set_cors(500)
            request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


    def clear(self, request):
        content_length = int(request.headers.get("Content-Length", 0))
        body = request.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            sid = str(req_data.get("session_id") or req_data.get("id") or "default").strip()
            self.forget(sid)
            if sid in self.deleted or sid not in self.memory:
                # Удалённую/отсутствующую сессию не «воскрешаем» пустым файлом.
                request._set_cors()
                request.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
                return
            # оставляем system-промпт, чистим только диалог
            kept = []
            if self.memory[sid] and isinstance(self.memory[sid][0], dict) and self.memory[sid][0].get("role") == "system":
                kept = self.memory[sid][:1]
            self.memory[sid] = kept
            self.save_all(self.memory)
            request._set_cors()
            request.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
        except Exception as e:
            request._set_cors(500)
            request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


