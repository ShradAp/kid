"""Independent Document Studio profiles, sessions and metadata."""

from datetime import datetime
from bridge_security import retained_secret
from config_manager import load_studio_history, save_studio_history, purge_studio_session_storage

STUDIO_ACTIVE_KEY = "studio_active_profile"
STUDIO_META_KEY = "studio_sessions_meta"
STUDIO_PROFILE_FLAG = "is_studio"
STUDIO_DEFAULT_PROFILE = "studio"
STUDIO_SESSION_ID = "document-studio"


def _is_studio_profile(data: object) -> bool:
    """Профиль принадлежит студии (флаг is_studio в общем config["profiles"])."""
    return isinstance(data, dict) and bool(data.get(STUDIO_PROFILE_FLAG))


def _studio_persona(p_id: str, data: dict, active: str = "") -> dict:
    """Профиль студии в форме фронтенда (как персона основного чата)."""
    return {
        "id": p_id,
        "name": str(data.get("display_name") or "").strip() or p_id.upper(),
        "emoji": str(data.get("emoji") or "📄"),
        "description": str(data.get("description") or ""),
        "modelName": str(data.get("model", "")),
        "systemPrompt": str(data.get("system_prompt", "")),
        "temperature": float(data.get("temperature", 0.7)),
        "baseUrl": str(data.get("base_url") or ""),
        "apiKey": "",
        "hasApiKey": bool(data.get("api_key") or data.get("api_key_env")),
        "requestTimeout": int(data.get("request_timeout", 120) or 120),
        "accentColor": "#6366f1" if p_id == active else "#64748b",
    }


def _studio_profile_from_persona(persona: dict, existing: dict | None = None) -> dict:
    """Преобразует профиль студии из формы фронтенда в запись config.json."""
    return {
        STUDIO_PROFILE_FLAG: True,
        "display_name": str(persona.get("name") or "").strip(),
        "emoji": str(persona.get("emoji") or "📄"),
        "description": str(persona.get("description") or ""),
        "base_url": str(persona.get("baseUrl") or ""),
        "model": str(persona.get("modelName") or ""),
        "api_key": retained_secret(persona.get("apiKey"), (existing or {}).get("api_key"), persona.get("clearApiKey")),
        "system_prompt": str(persona.get("systemPrompt") or ""),
        "temperature": float(persona.get("temperature", 0.7) or 0.7),
        "request_timeout": int(persona.get("requestTimeout", 120) or 120),
    }


class StudioSessions:
    def __init__(self, load_config, save_config, log):
        self.load_config = load_config
        self.save_config = save_config
        self.log = log
        self.history = {}
        self.metadata = {}
        self.deleted = set()

    def read_metadata(self) -> dict:
        """Метаданные чатов студии (id -> {personaId, title, timestamp})."""
        try:
            metadata = self.load_config().get(STUDIO_META_KEY, {})
            return metadata if isinstance(metadata, dict) else {}
        except Exception as e:
            self.log(f"[!] Ошибка чтения метаданных чатов студии: {e}")
            return {}


    def write_metadata(self, meta: dict) -> None:
        """Сохраняет метаданные чатов студии в config.json."""
        try:
            config = self.load_config()
            config[STUDIO_META_KEY] = meta if isinstance(meta, dict) else {}
            self.save_config(config)
        except Exception as e:
            self.log(f"[!] Ошибка записи метаданных чатов студии: {e}")


    def make_profile(self, base: dict | None = None) -> dict:
        """Стартовый профиль студии: копия базового профиля + пометка студии."""
        profile = dict(base or {})
        # Studio is an independent API-only profile; never copy Codex proxy secrets
        # or silently interpret a Codex profile as an API connection.
        profile["engine"] = "api"
        profile.pop("codex", None)
        profile[STUDIO_PROFILE_FLAG] = True
        profile["display_name"] = str(profile.get("display_name") or "").strip() or "Документ-студия"
        profile.setdefault("emoji", "📄")
        ds = self.load_config().get("document_studio")
        if isinstance(ds, dict):
            if str(ds.get("base_url") or "").strip():
                profile["base_url"] = str(ds["base_url"]).strip()
            if str(ds.get("api_key") or "").strip():
                profile["api_key"] = str(ds["api_key"]).strip()
            if str(ds.get("model") or "").strip():
                profile["model"] = str(ds["model"]).strip()
            if str(ds.get("system_prompt") or "").strip():
                profile["system_prompt"] = str(ds["system_prompt"])
            if isinstance(ds.get("temperature"), (int, float)):
                profile["temperature"] = float(ds["temperature"])
            if isinstance(ds.get("request_timeout"), (int, float)) and ds["request_timeout"]:
                profile["request_timeout"] = int(ds["request_timeout"])
        return profile


    def ensure_profiles(self) -> None:
        """Гарантирует наличие хотя бы одного профиля студии (иначе создаёт стартовый)."""
        config = self.load_config()
        profiles = config.get("profiles") or {}
        studio = {k: v for k, v in profiles.items() if _is_studio_profile(v)}
        if studio:
            if config.get(STUDIO_ACTIVE_KEY) not in studio:
                config[STUDIO_ACTIVE_KEY] = next(iter(studio))
                self.save_config(config)
            return
        base = dict(profiles.get(str(config.get("active_profile", "")) or "") or {})
        profiles[STUDIO_DEFAULT_PROFILE] = self.make_profile(base)
        config["profiles"] = profiles
        config[STUDIO_ACTIVE_KEY] = STUDIO_DEFAULT_PROFILE
        self.save_config(config)
        self.log("[STUDIO] создан стартовый профиль студии 'studio'")


    def profiles(self) -> dict:
        """Только профили студии из общего config["profiles"]."""
        profiles = self.load_config().get("profiles") or {}
        return {k: v for k, v in profiles.items() if _is_studio_profile(v)}


    def active_name(self) -> str:
        return str(self.load_config().get(STUDIO_ACTIVE_KEY) or "").strip()


    def init_payload(self) -> dict:
        """Профили студии + список её чатов (отдельно от основного чата)."""
        self.ensure_profiles()
        config = self.load_config()
        profiles = self.profiles()
        active = str(config.get(STUDIO_ACTIVE_KEY) or "")
        personas = [_studio_persona(pid, data, active) for pid, data in profiles.items()]
        meta = self.read_metadata()
        ids = set(meta.keys())
        try:
            from config_manager import list_studio_history_ids

            ids.update(list_studio_history_ids())
        except Exception:  # noqa: BLE001 - отсутствие каталога не критично
            pass
        sessions = []
        for sid in ids:
            if sid in self.deleted:
                continue
            info = meta.get(sid) or {}
            sessions.append(
                {
                    "id": sid,
                    "title": str(info.get("title") or "Новый чат"),
                    "personaId": str(info.get("personaId") or active),
                    "timestamp": str(info.get("timestamp") or ""),
                }
            )
        sessions.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
        return {
            "profiles": personas,
            "active_profile": active,
            "sessions": sessions,
            "last_active_session": str(config.get("studio_last_session") or ""),
        }


    def handle(self, path: str, req_data: dict) -> dict:
        """Обрабатывает действия студии: профили и чаты (отдельное хранилище)."""
        if path == "/api/studio/save_profile":
            persona = req_data.get("persona")
            if not isinstance(persona, dict) or not str(persona.get("id") or "").strip():
                raise ValueError("persona.id required")
            self.ensure_profiles()
            config = self.load_config()
            profiles = config.get("profiles") or {}
            pid = str(persona["id"]).strip()
            existing = profiles.get(pid, {})
            if existing and not _is_studio_profile(existing):
                raise ValueError("Cannot overwrite a main profile from Document Studio")
            profiles[pid] = _studio_profile_from_persona(persona, existing)
            config["profiles"] = profiles
            if config.get(STUDIO_ACTIVE_KEY) not in self.profiles():
                config[STUDIO_ACTIVE_KEY] = pid
            self.save_config(config)
            return {"ok": True}

        if path == "/api/studio/delete_profile":
            pid = str(req_data.get("profile_id") or "").strip()
            config = self.load_config()
            profiles = config.get("profiles") or {}
            if _is_studio_profile(profiles.get(pid)):
                del profiles[pid]
            config["profiles"] = profiles
            self.save_config(config)
            self.ensure_profiles()
            return {"ok": True, "active_profile": self.active_name()}

        if path == "/api/studio/set_active_profile":
            pid = str(req_data.get("profile_id") or "").strip()
            config = self.load_config()
            profiles = config.get("profiles") or {}
            if _is_studio_profile(profiles.get(pid)):
                config[STUDIO_ACTIVE_KEY] = pid
                self.save_config(config)
            return {"ok": True, "active_profile": pid}

        if path == "/api/studio/session/upsert":
            sid = str(req_data.get("id") or req_data.get("session_id") or "").strip()
            if not sid:
                raise ValueError("id required")
            self.deleted.discard(sid)
            meta = self.read_metadata()
            cur = meta.get(sid) or {}
            pid = str(
                req_data.get("personaId")
                or req_data.get("persona_id")
                or cur.get("personaId")
                or self.active_name()
                or "studio"
            )
            title = (req_data.get("title") or cur.get("title") or "Новый чат").strip() or "Новый чат"
            meta[sid] = {
                "personaId": pid,
                "title": title,
                "timestamp": req_data.get("timestamp")
                or cur.get("timestamp")
                or datetime.now().strftime("%d.%m %H:%M"),
            }
            self.metadata[sid] = meta[sid]
            self.write_metadata(meta)
            if sid not in self.history:
                self.history[sid] = load_studio_history(sid)
            return {"ok": True, "session": {"id": sid, **meta[sid]}}

        if path == "/api/studio/session/delete":
            sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
            if not sid:
                raise ValueError("session_id required")
            self.deleted.add(sid)
            self.history.pop(sid, None)
            self.metadata.pop(sid, None)
            for item in purge_studio_session_storage(sid):
                self.log(f"[STUDIO DELETE] Стёрто: {item}")
            meta = self.read_metadata()
            if sid in meta:
                del meta[sid]
                self.write_metadata(meta)
            return {"ok": True, "session_id": sid}

        if path == "/api/studio/session/clear":
            sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
            if not sid:
                raise ValueError("session_id required")
            messages = self.history.get(sid)
            if messages is None:
                messages = load_studio_history(sid)
            kept = (
                messages[:1]
                if messages
                and isinstance(messages[0], dict)
                and messages[0].get("role") == "system"
                else []
            )
            self.history[sid] = kept
            save_studio_history(sid, kept)
            return {"ok": True}

        if path == "/api/studio/session/messages":
            sid = str(req_data.get("session_id") or req_data.get("id") or "").strip()
            if not sid:
                raise ValueError("session_id required")
            messages = self.history.get(sid)
            if messages is None:
                messages = load_studio_history(sid)
            return {"messages": messages}

        raise ValueError(f"Неизвестный запрос студии: {path}")


