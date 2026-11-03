"""Authorized local library endpoints. Inspect/install never execute a package."""
import copy
import json
from pathlib import Path
import urllib.parse

import codex_engine
from codex_service import SessionBindings
from services import extensions


def handle(request, method, *, root, load_config, save_config, service, sessions):
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query, keep_blank_values=True)
        body = json.loads(request.rfile.read(int(request.headers.get("Content-Length", "0"))) or b"{}") if method == "POST" else {}
        if not isinstance(body, dict):
            raise ValueError("Ожидается объект JSON")
        action = body.get("action", "list")
        profile_id = str(body.get("profile_id") or query.get("profile", [""])[0])
        with extensions.LOCK:
            cfg = load_config()
            profile = cfg.get("profiles", {}).get(profile_id, {})
            enabled = profile.get("extension_ids", []) if profile_id else None
            provider = codex_engine.engine(profile) if profile_id else "codex"
            if action == "select":
                if not profile_id or profile_id not in cfg.get("profiles", {}):
                    raise ValueError("Сначала сохраните новый профиль")
                selected = extensions.selection(body.get("selected"), cfg.get("extensions", []))
                profile["extension_ids"] = selected
                save_config(cfg)
                result = {"ok": True, "selected": selected, "apply": "next_message"}
            elif action == "preview":
                preview = extensions.inspect_source(body.get("source", ""), body.get("type"),
                    str(body.get("name", "")), str(body.get("description", "")))
                result = {"preview": preview, "fingerprint": extensions.digest(preview)}
            elif action == "install":
                preview = extensions.inspect_source(body.get("source", ""), body.get("type"),
                    str(body.get("name", "")), str(body.get("description", "")))
                if extensions.digest(preview) != body.get("fingerprint"):
                    raise ValueError("Источник изменился. Сначала повторите предварительный просмотр")
                extensions.install(root, cfg, preview)
                save_config(cfg)
                result = {"ok": True}
            elif action in ("remove", "configure", "probe"):
                item = next((item for item in cfg.get("extensions", []) if item["id"] == body.get("id")), None)
                if item is None:
                    raise ValueError("Расширение не найдено")
                if action == "remove":
                    cfg["extensions"] = [entry for entry in cfg["extensions"] if entry["id"] != item["id"]]
                    for data in cfg.get("profiles", {}).values():
                        data["extension_ids"] = [identity for identity in data.get("extension_ids", []) if identity != item["id"]]
                    # Session snapshots have their own copies, so active instructions survive removal.
                    destination = Path(root) / "extensions" / item["id"]
                    if destination.resolve().is_relative_to((Path(root) / "extensions").resolve()) and destination.exists():
                        import shutil
                        shutil.rmtree(destination)
                elif action == "configure":
                    if "description" in body:
                        description = str(body["description"]).strip()
                        if not description or len(description) > 500:
                            raise ValueError("Описание должно содержать от 1 до 500 символов")
                        item["description"] = description
                        if item["type"] == "skill":
                            for skill in item.get("skills", []):
                                skill["description"] = description
                    if "mcp_servers" in body:
                        connections = body["mcp_servers"]
                        if not isinstance(connections, dict) or set(connections) != set(item.get("mcp_servers", {})):
                            raise ValueError("Настройка должна сохранять состав компонентов MCP")
                        item["mcp_servers"] = {name: extensions._connection(value) for name, value in sorted(connections.items())}
                        item["probe"] = {}
                    references = {reference for connection in item.get("mcp_servers", {}).values() for reference in connection.get("env_from", {}).values()}
                    secrets = body.get("secrets", {})
                    clear = body.get("clear_secrets", [])
                    if not isinstance(secrets, dict) or not isinstance(clear, list) or (set(secrets) | set(clear)) - references:
                        raise ValueError("Неизвестные имена секретов MCP")
                    from bridge_security import retained_secret
                    for reference in references:
                        if reference in secrets or reference in clear:
                            if reference in secrets and not isinstance(secrets[reference], str):
                                raise ValueError("Секрет должен быть строкой")
                            store = cfg.setdefault("extension_secrets", {})
                            store[reference] = retained_secret(secrets.get(reference), store.get(reference), reference in clear)
                            item["probe"] = {}
                else:
                    # Release the config lock while a user-authorized MCP process is probed.
                    target, specification = item["id"], copy.deepcopy(item)
                    values = extensions.secret_values(cfg)
                    result = None
                if action != "probe":
                    save_config(cfg)
                    result = {"ok": True}
            elif action == "restart":
                session_id = str(body.get("session_id") or "")
                meta = sessions.read_metadata().get(session_id, {})
                if not profile_id or profile_id not in cfg.get("profiles", {}) or meta.get("personaId") != profile_id:
                    raise ValueError("Сессия не принадлежит этому профилю")
                service.get(profile_id, profile).restart_extensions(session_id)
                result = {"ok": True}
            elif action == "list":
                chat_sessions = []
                bound_threads = set()
                snapshot_errors = []
                if profile_id:
                    bindings = SessionBindings(codex_engine.profile_home(root, profile_id) / "bindings")
                    for identity, meta in sessions.read_metadata().items():
                        if meta.get("personaId") != profile_id:
                            continue
                        binding = bindings.read(identity)
                        path = binding.get("extension_snapshot")
                        if not binding.get("thread_id"):
                            continue
                        bound_threads.add(binding["thread_id"])
                        pending = False
                        if path:
                            try:
                                snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
                                pending = snapshot.get("selection") != sorted(enabled or []) or snapshot.get("library") != extensions.catalog_revision(cfg, enabled or [])
                                snapshot_errors.extend(snapshot.get("errors", []))
                            except (ValueError, OSError):
                                pending = True
                        elif enabled:
                            pending = True
                        chat_sessions.append({"id": identity, "title": meta.get("title", identity), "pending": pending,
                            "busy": binding.get("status") in ("pending", "active", "uncertain")})
                status_selection = query["selected"][0].split(",") if "selected" in query and query["selected"][0] else ([] if "selected" in query else enabled)
                status_provider = query.get("provider", [provider])[0]
                if status_provider not in ("codex", "api"):
                    status_provider = provider
                result = {"extensions": extensions.states(cfg, status_selection, status_provider), "selected": enabled or [],
                          "provider": provider, "sessions": chat_sessions, "profile_exists": profile_id in cfg.get("profiles", {}),
                          "apply": "next_message",
                          "has_secrets": {reference: reference in extensions.secret_values(cfg) for item in cfg.get("extensions", [])
                              for connection in item.get("mcp_servers", {}).values() for reference in connection.get("env_from", {}).values()}}
                saved = service.sessions.get(profile_id) if profile_id else None
                if saved:
                    with saved[1].lock:
                        failed = [name for (thread, name), status in saved[1].extension_runtime_states.items()
                                  if thread in bound_threads and status in ("failed", "cancelled")]
                    for item in result["extensions"]:
                        for server in failed:
                            if server.startswith("ext_" + item["id"] + "_"):
                                item["status"] = "error"
                                item["reasons"].append("Ошибка подключения MCP в открытой сессии: " + server.rsplit("_", 1)[-1])
                for error in snapshot_errors:
                    for item in result["extensions"]:
                        if item["id"] == error["id"]:
                            item["status"] = "error"
                            item["reasons"].append(error["error"])
            else:
                raise ValueError("Неизвестное действие")
        if action == "probe":
            probes = extensions.probe(specification, values)
            with extensions.LOCK:
                cfg = load_config()
                item = next((item for item in cfg.get("extensions", []) if item["id"] == target), None)
                if item is None or item.get("mcp_servers") != specification.get("mcp_servers"):
                    raise ValueError("Настройки изменились во время проверки. Повторите проверку")
                item["probe"] = probes
                if not item.get("version") and len(probes) == 1:
                    item["version"] = next(iter(probes.values())).get("server_info", {}).get("version", "")
                save_config(cfg)
            result = {"ok": True, "probe": probes}
        request._set_cors()
        request.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except (ValueError, OSError, KeyError, TypeError, codex_engine.ProtocolError) as error:
        request._set_cors(400)
        message = str(error) if isinstance(error, (ValueError, codex_engine.ProtocolError)) else "Источник или настройки расширения недоступны"
        request.wfile.write(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
