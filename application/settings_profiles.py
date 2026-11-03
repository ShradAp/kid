"""Profile and settings HTTP workflows with explicit application dependencies."""

import json
import os
import threading
import urllib.parse

import codex_engine
from bridge_security import retained_secret
from application.studio_profiles import _is_studio_profile
from application.session_history import _msg_text


def initial_state(request, sessions, read_history, get_balance, read_notes, load_config):
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    # Основной чат видит только НЕ-студийные профили: профили студии
    # скрыты здесь и показываются лишь при открытой студии.
    main_profiles = {
        name: data for name, data in profiles.items() if not _is_studio_profile(data)
    }
    active_profile_key = cfg.get("active_profile", "main")
    if active_profile_key not in main_profiles:
        active_profile_key = next(iter(main_profiles), "main")

    personas = []
    for p_id, p_data in main_profiles.items():
        personas.append(
            {
                "id": p_id,
                "engine": p_data.get("engine", "api"),
                "codex": codex_engine.public_settings(p_data),
                # Отображаемое имя/эмодзи/описание профиля хранятся в config.json
                # (display_name/emoji/description) и переживают перезапуск;
                # без сохранённого имени показываем, как раньше, id в верхнем регистре.
                "name": (str(p_data.get("display_name") or "").strip() or p_id.upper()),
                "emoji": str(p_data.get("emoji") or "🤖"),
                "color": p_data.get("color", "auto"),
                "description": str(p_data.get("description") or ""),
                "tagline": p_data.get("model", ""),
                "modelName": p_data.get("model", ""),
                "systemPrompt": p_data.get("system_prompt", ""),
                "temperature": float(p_data.get("temperature", 0.7)),
                "baseUrl": p_data.get("base_url")
                or p_data.get("api_base", "https://sprutdock.ru/v1"),
                "apiKey": "",
                "hasApiKey": bool(p_data.get("api_key") or p_data.get("api_key_env")),
                "thinkingBudget": int(p_data.get("thinking_budget", 0)),

                "maxSteps": max(1, min(200, int(p_data.get("max_steps", 60) or 60))),
                "attachMaxImages": max(1, int(p_data.get("attach_max_images", 4) or 4)),
                "attachMaxTextChars": max(100, int(p_data.get("attach_max_text_chars", 60000) or 60000)),
                "attachMaxTextPerFile": max(100, int(p_data.get("attach_max_text_per_file", 60000) or 60000)),
                "attachMaxTotalText": max(100, int(p_data.get("attach_max_total_text", 180000) or 180000)),
                "attachCsvMaxRows": max(1, int(p_data.get("attach_csv_max_rows", 400) or 400)),
                "attachXlsxMaxRows": max(1, int(p_data.get("attach_xlsx_max_rows", 400) or 400)),
                "chatMsgMaxChars": max(0, int(p_data.get("chat_msg_max_chars", 0) or 0)),
                "chatMsgTextPartMaxChars": max(0, int(p_data.get("chat_msg_text_part_max_chars", 0) or 0)),
                "accentColor": "#6366f1"
                if p_id == active_profile_key
                else "#64748b",
            }
        )

    active_data = profiles.get(active_profile_key, {})
    current_balance = None if codex_engine.engine(active_data) == "codex" else get_balance(
        active_data.get("api_key", ""), active_data.get("base_url", "")
    )

    history_data = read_history()
    # подтягиваем мету сессий (привязка чат -> профиль + заголовки)
    try:
        meta_data = sessions.read_metadata()
    except Exception:
        meta_data = {}
    if not isinstance(meta_data, dict):
        meta_data = {}
    # каждая сессия с диска обязана иметь мету, иначе фронт её не покажет
    meta_dirty = False
    # Чаты, удалённые в этом запуске не должны воскресать в списке:
    # иначе поздний /api/init вернёт стёртую сессию обратно в UI.
    for _sid in [s for s in list(history_data.keys()) if s in sessions.deleted]:
        history_data.pop(_sid, None)
    for _sid in [s for s in list(meta_data.keys()) if s in sessions.deleted]:
        meta_data.pop(_sid, None)
        meta_dirty = True
    for sid in list(history_data.keys()):
        m = meta_data.get(sid)
        if not isinstance(m, dict) or not m.get("personaId"):
            meta_data[sid] = {
                "personaId": active_profile_key,
                "title": "Основной диалог" if sid == "default" else ("Чат %s" % str(sid)[-6:]),
                "timestamp": "Ранее",
            }
            meta_dirty = True
    # пустые чаты, созданные фронтом (есть мета, но сообщений ещё нет)
    for sid in list(meta_data.keys()):
        if sid not in history_data:
            history_data[sid] = []
    if meta_dirty:
        try:
            sessions.write_metadata(meta_data)
        except Exception:
            pass
    default_history = history_data.get("default", [])
    total_chars = sum(
        len(_msg_text(m.get("content", ""))) for s in history_data.values() for m in s if isinstance(m, dict)
    )
    sessions_list = []
    session_messages = {}
    for sid, msgs in history_data.items():
        m = meta_data.get(sid) or {}
        sessions_list.append(
            {
                "id": sid,
                "personaId": m.get("personaId") or active_profile_key,
                "title": m.get("title") or ("Основной диалог" if sid == "default" else sid),
                "timestamp": m.get("timestamp") or "Ранее",
            }
        )
        clean = []
        for msg in (msgs or []):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            # display — сырой текст пользователя (без содержимого файлов):
            # content хранит промпт с вложениями для API, показывать
            # его в ленте целиком нельзя.
            _disp = msg.get("display")
            text = _msg_text(
                _disp if isinstance(_disp, str) and _disp.strip() else msg.get("content", "")
            )
            if not text.strip() and not msg.get("images"):
                continue
            entry = {"role": role, "content": text}
            if role == "assistant" and isinstance(msg.get("images"), list):
                entry["images"] = msg["images"]
            if isinstance(msg.get("attachments"), list) and msg["attachments"]:
                entry["attachments"] = msg["attachments"]
            if role == "assistant" and isinstance(msg.get("reasoning"), str) and msg["reasoning"].strip():
                entry["reasoning"] = msg["reasoning"]
            if role == "assistant":
                if isinstance(msg.get("thinking"), list):
                    entry["thinking"] = [str(step) for step in msg["thinking"]]
                if isinstance(msg.get("activity"), dict):
                    entry["activity"] = msg["activity"]
                elif msg.get("codex_operation"):
                    entry["activity"] = {}
            clean.append(entry)
        # свежие сессии первыми, default — всегда в списке
        session_messages[sid] = clean[-200:]
    # сортировка: default первый, остальные как есть (фронт сам отсортирует по времени)
    sessions_list.sort(key=lambda s: (s["id"] != "default", s["id"]), reverse=False)

    request._set_cors()
    payload = {
        "active_profile": active_profile_key,
        "search_api_key": "",
        "has_search_api_key": bool(cfg.get("search_api_key")),
        "search_research_mode": cfg.get("search_research_mode", "adaptive"),
        # Последний открытый чат — восстанавливается фронтендом после рестарта.
        "active_session": str(cfg.get("last_active_session") or ""),
        "personas": personas,
        "balance": current_balance,
        "used_tokens": total_chars // 4,
        "history": default_history,
        "sessions": sessions_list,
        "session_messages": session_messages,
        "notes": read_notes(),
    }
    request.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def save_persona(request, load_config, save_config, codex_service):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body)
        persona = req_data.get("persona")
        if not persona or not persona.get("id"):
            request._set_cors(400)
            request.wfile.write(json.dumps({"error": "persona.id required"}).encode("utf-8"))
            return
        cfg = load_config()
        profiles = cfg.get("profiles") or {}
        profile_name = persona["id"]
        existing = codex_engine.update_profile(profiles.get(profile_name, {}), persona)
        # Профиль студии редактируется только из студии — основные профили
        # не должны его перезаписывать.
        if _is_studio_profile(existing):
            request._set_cors(400)
            request.wfile.write(
                json.dumps({"error": "Это профиль документ-студии"}).encode("utf-8")
            )
            return
        existing.update({
            # отображаемые поля профиля: имя, эмодзи, описание
            "display_name": str(persona.get("name", existing.get("display_name", "")) or "").strip()
            or profile_name.upper(),
            "emoji": str(persona.get("emoji", existing.get("emoji", "🤖")) or "🤖"),
            "color": persona.get("color", existing.get("color", "auto")) if persona.get("color", existing.get("color", "auto")) in ("auto", "rose", "peach", "sand", "sage", "teal", "blue", "lavender") else "auto",
            "description": str(persona.get("description", existing.get("description", ""))),
            "base_url": persona.get("baseUrl", existing.get("base_url", "")),
            "model": persona.get("modelName", existing.get("model", "")),
            "api_key": retained_secret(persona.get("apiKey"), existing.get("api_key"), persona.get("clearApiKey")),
            "system_prompt": persona.get("systemPrompt", existing.get("system_prompt", "")),
            "temperature": float(persona.get("temperature", existing.get("temperature", 0.7))),
            "max_steps": max(1, min(200, int(persona.get("maxSteps", existing.get("max_steps", 60)) or 60))),
            "thinking_budget": int(persona.get("thinkingBudget", existing.get("thinking_budget", 0))),
            "attach_max_images": max(1, int(persona.get("attachMaxImages", existing.get("attach_max_images", 4)) or 4)),
            "attach_max_text_chars": max(100, int(persona.get("attachMaxTextChars", existing.get("attach_max_text_chars", 60000)) or 60000)),
            "attach_max_text_per_file": max(100, int(persona.get("attachMaxTextPerFile", existing.get("attach_max_text_per_file", 60000)) or 60000)),
            "attach_max_total_text": max(100, int(persona.get("attachMaxTotalText", existing.get("attach_max_total_text", 180000)) or 180000)),
            "attach_csv_max_rows": max(1, int(persona.get("attachCsvMaxRows", existing.get("attach_csv_max_rows", 400)) or 400)),
            "attach_xlsx_max_rows": max(1, int(persona.get("attachXlsxMaxRows", existing.get("attach_xlsx_max_rows", 400)) or 400)),
            "chat_msg_max_chars": max(0, int(persona.get("chatMsgMaxChars", existing.get("chat_msg_max_chars", 0)) or 0)),
            "chat_msg_text_part_max_chars": max(0, int(persona.get("chatMsgTextPartMaxChars", existing.get("chat_msg_text_part_max_chars", 0)) or 0)),
        })
        profiles[profile_name] = existing
        codex_service.errors.pop(profile_name, None)
        if persona.get("clearApiKey") is True:
            existing["api_key_env"] = ""
        cfg["profiles"] = profiles
        save_config(cfg)
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
    except Exception as e:
        request._set_cors(500)
        request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


def delete_persona(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body)
        profile_id = str(req_data.get("profile_id", "")).strip()
        cfg = load_config()
        profiles = cfg.get("profiles") or {}
        if not profile_id or profile_id not in profiles:
            request._set_cors(400)
            request.wfile.write(json.dumps({"error": "profile_id required"}).encode("utf-8"))
            return
        # Профили студии удаляются из студии, не из основного окна.
        if _is_studio_profile(profiles.get(profile_id)):
            request._set_cors(400)
            request.wfile.write(
                json.dumps({"error": "Профиль документ-студии удаляется в студии"}).encode("utf-8")
            )
            return
        main_profiles = {
            name: data for name, data in profiles.items() if not _is_studio_profile(data)
        }
        if len(main_profiles) <= 1:
            request._set_cors(400)
            request.wfile.write(json.dumps({"error": "Нельзя удалить единственный профиль"}).encode("utf-8"))
            return
        del profiles[profile_id]
        cfg["profiles"] = profiles
        if cfg.get("active_profile") == profile_id:
            cfg["active_profile"] = next(
                (name for name in profiles if not _is_studio_profile(profiles[name])),
                cfg.get("active_profile"),
            )
        save_config(cfg)
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True, "active_profile": cfg.get("active_profile")}).encode("utf-8"))
    except Exception as e:
        request._set_cors(500)
        request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


def set_active_profile(request, load_config, save_config, codex_service, connect_codex):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body)
        profile_id = req_data.get("profile_id", "")
        cfg = load_config()
        profiles = cfg.get("profiles") or {}
        # Основной чат не может сделать активным профиль студии.
        if profile_id in profiles and not _is_studio_profile(profiles.get(profile_id)):
            changed = cfg.get("active_profile") != profile_id
            cfg["active_profile"] = profile_id
            save_config(cfg)
            if codex_engine.engine(profiles[profile_id]) == "codex" and (changed or profile_id not in codex_service.sessions):
                threading.Thread(target=connect_codex, daemon=True, name="kid-codex-profile-connect").start()
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
    except Exception as e:
        request._set_cors(500)
        request.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


def reveal_secret(request, load_config):
    req = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))).decode("utf-8"))
    cfg = load_config()
    field = req.get("field")
    if field in ("profile_api_key", "codex_proxy_password"):
        profile = (cfg.get("profiles") or {}).get(str(req.get("profile_id") or ""))
        if not isinstance(profile, dict) or _is_studio_profile(profile):
            request._set_cors(404)
            request.wfile.write(b'{"error":"Profile not found"}')
            return
        secret = (profile.get("api_key") or os.environ.get(str(profile.get("api_key_env") or ""), "")) if field == "profile_api_key" else (profile.get("codex") or {}).get("proxy_password", "")
    elif field == "search_api_key":
        secret = cfg.get("search_api_key", "")
    elif field == "proxy_password":
        secret = cfg.get("proxy_pass") or urllib.parse.unquote(urllib.parse.urlparse(cfg.get("proxy_url", "")).password or "")
    else:
        request._set_cors(400)
        request.wfile.write(b'{"error":"Unknown secret field"}')
        return
    # Только явный запрос глазика; обычная загрузка настроек не раскрывает секреты.
    request._set_cors()
    request.wfile.write(json.dumps({"secret": secret}, ensure_ascii=False).encode("utf-8"))
    return



def get_studio_settings(request, load_config):
    cfg = load_config()
    settings = cfg.get("document_studio", {})
    if not isinstance(settings, dict):
        settings = {}
    request._set_cors()
    request.wfile.write(
        json.dumps(
            {
                "base_url": str(settings.get("base_url", "")),
                "api_key": "",
                "has_api_key": bool(settings.get("api_key")),
                "model": str(settings.get("model", "")),
                "system_prompt": str(settings.get("system_prompt", "")),
                "temperature": settings.get("temperature"),
                "request_timeout": settings.get("request_timeout") or 0,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )
    return


def save_studio_settings(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        incoming = json.loads(body) if body else {}
        if not isinstance(incoming, dict):
            raise ValueError("Некорректные настройки документ-студии")

        def _num(value):
            if value in ("", None):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        cfg = load_config()
        cfg["document_studio"] = {
            "base_url": str(incoming.get("base_url", "") or "").strip(),
            "api_key": retained_secret(incoming.get("api_key"), cfg.get("document_studio", {}).get("api_key"), incoming.get("clear_api_key")),
            "model": str(incoming.get("model", "") or "").strip(),
            "system_prompt": str(incoming.get("system_prompt", "") or ""),
            "temperature": _num(incoming.get("temperature")),
            "request_timeout": int(_num(incoming.get("request_timeout")) or 0),
        }
        save_config(cfg)
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - ошибку показываем фронтенду
        request._set_cors(400)
        request.wfile.write(
            json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
        )
    return


def get_prompt_generator_settings(request, load_config):
    cfg = load_config()
    settings = cfg.get("prompt_generator", {})
    if not isinstance(settings, dict):
        settings = {}
    request._set_cors()
    request.wfile.write(
        json.dumps(
            {
                "base_url": str(settings.get("base_url", "")),
                "api_key": "",
                "has_api_key": bool(settings.get("api_key")),
                "model": str(settings.get("model", "")),
                "system_prompt": str(settings.get("system_prompt", "")),
                "idea": str(settings.get("idea", "")),
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )


def save_prompt_generator_settings(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        incoming = json.loads(body)
        if not isinstance(incoming, dict):
            raise ValueError("Некорректные настройки генератора")
        cfg = load_config()
        cfg["prompt_generator"] = {
            "base_url": str(incoming.get("base_url", "")),
            "api_key": retained_secret(incoming.get("api_key"), cfg.get("prompt_generator", {}).get("api_key"), incoming.get("clear_api_key")),
            "model": str(incoming.get("model", "")),
            "system_prompt": str(incoming.get("system_prompt", "")),
            "idea": str(incoming.get("idea", "")),
        }
        save_config(cfg)
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return


def get_file_click_mode(request, load_config):
    cfg = load_config()
    mode = str(cfg.get("file_click_mode", "download") or "download")
    if mode not in ("download", "preview"):
        mode = "download"
    request._set_cors()
    request.wfile.write(
        json.dumps({"mode": mode}, ensure_ascii=False).encode("utf-8")
    )


def save_file_click_mode(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body) if body else {}
        if not isinstance(req_data, dict):
            raise ValueError("Некорректный запрос режима файлов")
        mode = str(req_data.get("mode", "")).strip().lower()
        if mode not in ("download", "preview"):
            raise ValueError("Неизвестный режим: %s" % mode)
        cfg = load_config()
        cfg["file_click_mode"] = mode
        save_config(cfg)
        request._set_cors()
        request.wfile.write(
            json.dumps({"ok": True, "mode": mode}, ensure_ascii=False).encode("utf-8")
        )
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return


def get_tool_send_mode(request, load_config):
    cfg = load_config()
    mode = str(cfg.get("tool_send_mode", "gated") or "gated").strip().lower()
    if mode not in ("gated", "all", "embedding"):
        mode = "gated"
    request._set_cors()
    request.wfile.write(
        json.dumps({"mode": mode}, ensure_ascii=False).encode("utf-8")
    )


def save_tool_send_mode(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body) if body else {}
        if not isinstance(req_data, dict):
            raise ValueError("Некорректный запрос режима отправки тулов")
        mode = str(req_data.get("mode", "gated") or "gated").strip().lower()
        if mode not in ("gated", "all", "embedding"):
            mode = "gated"
        cfg = load_config()
        cfg["tool_send_mode"] = mode
        save_config(cfg)
        request._set_cors()
        request.wfile.write(
            json.dumps({"ok": True, "mode": mode}, ensure_ascii=False).encode("utf-8")
        )
    except Exception as exc:  # noqa: BLE001
        request._set_cors(400)
        request.wfile.write(
            json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
        )
    return


def get_download_base_path(request, load_config):
    cfg = load_config()
    base = str(cfg.get("download_base_path", "") or "").strip()
    request._set_cors()
    request.wfile.write(
        json.dumps({"path": base}, ensure_ascii=False).encode("utf-8")
    )


def save_download_base_path(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body) if body else {}
        if not isinstance(req_data, dict):
            raise ValueError("Некорректный запрос пути скачивания")
        raw = str(req_data.get("path", "") or "").strip()
        base = os.path.expanduser(raw).strip() if raw else ""
        cfg = load_config()
        if base:
            cfg["download_base_path"] = base
        else:
            cfg.pop("download_base_path", None)
        save_config(cfg)
        request._set_cors()
        request.wfile.write(
            json.dumps({"ok": True, "path": base}, ensure_ascii=False).encode("utf-8")
        )
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return


def save_search_settings(request, load_config, save_config):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        incoming = json.loads(body)
        if not isinstance(incoming, dict):
            raise ValueError("Некорректные настройки поиска")
        cfg = load_config()
        cfg["search_api_key"] = retained_secret(incoming.get("search_api_key"), cfg.get("search_api_key"), incoming.get("clear_search_api_key"))
        if "search_research_mode" in incoming:
            from research_controller import MODES
            if incoming["search_research_mode"] not in MODES:
                raise ValueError("Неизвестный режим исследования")
            cfg["search_research_mode"] = incoming["search_research_mode"]
        save_config(cfg)
        request._set_cors()
        request.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return


def read_prompt_generator_settings_legacy(request, load_config):
    cfg = load_config()
    settings = cfg.get("prompt_generator", {})
    if not isinstance(settings, dict):
        settings = {}
    request._set_cors()
    request.wfile.write(json.dumps(settings, ensure_ascii=False).encode("utf-8"))
    return


def generate_prompt(request, load_config, save_config, call_chat_api):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body)
        base_url = str(req_data.get("base_url", "")).strip()
        api_key = str(req_data.get("api_key") or load_config().get("prompt_generator", {}).get("api_key", "")).strip()
        model = str(req_data.get("model", "")).strip()
        idea = str(req_data.get("idea", "")).strip()
        system_prompt = str(req_data.get("system_prompt", "")).strip()
        if not all((base_url, api_key, model, idea)):
            raise ValueError("Заполните Base URL, API key, Model и описание идеи")
        cfg = load_config()
        cfg["prompt_generator"] = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "system_prompt": system_prompt,
        }
        save_config(cfg)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": f"Составь подробное техническое задание по идее: {idea}"})
        response = call_chat_api(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=0.7,
            timeout=60,
        )
        text = response.get("content", "") if isinstance(response, dict) else str(response)
        if not text:
            raise ValueError("Модель вернула пустой ответ")
        request._set_cors()
        request.wfile.write(json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return



def get_proxy_settings(request, load_config):
    cfg = load_config()
    full = str(cfg.get("proxy_url", "") or "")
    server, username, password = full, "", ""
    if full:
        parsed = urllib.parse.urlparse(full)
        if parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            server = f"{parsed.scheme or 'http'}://{netloc}"
            username = parsed.username
            password = parsed.password or ""
    request._set_cors()
    request.wfile.write(
        json.dumps(
            {
                "mode": str(cfg.get("proxy_mode", "auto") or "auto"),
                "url": server,
                "username": username,
                "password": "",
                "has_password": bool(password or cfg.get("proxy_pass")),
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )


def save_proxy_settings(request, load_config, save_config, codex_service, connect_codex, log_event):
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length).decode("utf-8")
    try:
        req_data = json.loads(body)
        mode = str(req_data.get("mode", "auto")).strip().lower() or "auto"
        if mode not in ("auto", "direct", "custom"):
            raise ValueError(f"неизвестный режим: {mode}")
        address = str(req_data.get("url", "") or "").strip()
        username = str(req_data.get("username", "") or "").strip()
        previous = load_config()
        old_password = previous.get("proxy_pass") or urllib.parse.urlparse(previous.get("proxy_url", "")).password
        password = retained_secret(req_data.get("password"), old_password, req_data.get("clear_password"))
        timezone = str(req_data.get("timezone", "") or "").strip()
        locale = str(req_data.get("locale", "") or "").strip()
        accept_language = str(req_data.get("accept_language", "") or "").strip()
        if mode == "custom" and not address:
            raise ValueError("Адрес прокси обязателен")

        from network_agent.proxy_env import assemble_proxy

        full_url = assemble_proxy(address, username, password)
        cfg = load_config()
        cfg["proxy_mode"] = mode
        cfg["proxy_url"] = full_url if mode == "custom" else ""
        cfg["proxy_user"] = username if mode == "custom" else ""
        cfg["proxy_pass"] = password if mode == "custom" else ""
        cfg["proxy_timezone"] = timezone or "Europe/Tallinn"
        cfg["proxy_locale"] = locale or "et-EE"
        cfg["proxy_accept_language"] = accept_language
        save_config(cfg)
        codex_service.close()
        threading.Thread(target=connect_codex, daemon=True, name="kid-codex-proxy-connect").start()

        applied = False
        verified = True
        detail = ""
        try:
            # Ленивый импорт: тянет playwright при первом обращении.
            from network_agent.core import agent_core

            result = agent_core.apply_proxy_settings(
                mode,
                address,
                username,
                password,
                timezone,
                locale,
                accept_language,
                log_cb=lambda msg: log_event(f"[NET] {msg}"),
            )
            applied = True
            verified = bool(result.get("verified"))
            detail = str(result.get("detail", ""))
        except Exception as exc:
            detail = (
                "настройки сохранены, агент ещё не запущен — применятся "
                f"при первом чтении страницы ({exc})"
            )
        request._set_cors()
        request.wfile.write(
            json.dumps(
                {
                    "ok": True,
                    "mode": mode,
                    "applied": applied,
                    "verified": verified,
                    "detail": detail,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
    except Exception as exc:
        request._set_cors(400)
        request.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
    return


