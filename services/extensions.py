"""Shared local extension library, explicit dependencies and immutable chat snapshots.

Supported package manifest: kid-extension.json, format=kid-extension-v1,
name, description, version?, dependencies=[library IDs], skills=[relative paths],
mcp_servers={component: {command: absolute executable, args: [], env_from: {}}}.
No install scripts, downloads, inferred dependencies or global Codex settings.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import uuid
import yaml

from extensions_mcp import MCPClient

LOCK = threading.RLock()
TYPES = {"skill": "Скилл", "mcp": "MCP", "plugin": "Плагин"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def secret_values(cfg):
    return {**os.environ, **{key: value for key, value in cfg.get("extension_secrets", {}).items() if value}}


def catalog_revision(cfg, enabled):
    selected = set(enabled)
    return digest([item for item in sorted(cfg.get("extensions", []), key=lambda item: item["id"]) if item["id"] in selected])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Манифест содержит повторяющиеся поля; имена компонентов должны быть уникальны")
        result[key] = value
    return result


def selection(value, library):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Выбор расширений должен быть списком ID")
    known = {item["id"] for item in library}
    return sorted(set(value) & known)


def _local_path(value):
    path = Path(str(value)).expanduser()
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("Укажите абсолютный путь к локальному источнику")
    if not path.exists():
        raise ValueError("Локальный источник не найден")
    return path.resolve()


def _skill(path):
    path = path / "SKILL.md" if path.is_dir() else path
    if path.name != "SKILL.md" or path.stat().st_size > 256 * 1024:
        raise ValueError("Нужен SKILL.md размером до 256 КБ")
    text = path.read_text(encoding="utf-8-sig")
    metadata = {}
    if text.startswith("---\n"):
        try:
            metadata = yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError:
            raise ValueError("Некорректный YAML в SKILL.md") from None
        if not isinstance(metadata, dict):
            raise ValueError("Метаданные SKILL.md должны быть объектом")
    dependencies = metadata.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
        raise ValueError("dependencies в SKILL.md должен содержать ID библиотеки")
    declared_tools = []
    tools_file = path.parent / "agents" / "openai.yaml"
    if tools_file.is_file():
        if tools_file.stat().st_size > 256 * 1024:
            raise ValueError("agents/openai.yaml слишком большой")
        try:
            tool_metadata = yaml.safe_load(tools_file.read_text(encoding="utf-8")) or {}
            declared_tools = tool_metadata.get("dependencies", {}).get("tools", [])
        except (yaml.YAMLError, AttributeError):
            raise ValueError("Некорректные зависимости в agents/openai.yaml") from None
        if not isinstance(declared_tools, list) or any(not isinstance(item, dict) or not isinstance(item.get("value"), str) for item in declared_tools):
            raise ValueError("Некорректные зависимости инструментов скилла")
    return {"name": metadata.get("name", path.parent.name), "origin": str(path.resolve()),
            "description": metadata.get("description", ""), "path": str(path),
            "version": metadata.get("version", ""), "content_hash": _skill_fingerprint(path.parent),
            "dependencies": dependencies, "declared_tools": declared_tools}


def _skill_fingerprint(folder):
    files, total = [], 0
    for root, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs + names:
            item = Path(root) / name
            if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                raise ValueError("Ссылки и junction внутри скилла не поддерживаются")
        for name in sorted(names):
            item = Path(root) / name
            total += item.stat().st_size
            if total > 20 * 1024 * 1024 or len(files) >= 1000:
                raise ValueError("Скилл превышает лимит: 20 МБ или 1000 файлов")
            files.append((str(item.relative_to(folder)), hashlib.sha256(item.read_bytes()).hexdigest()))
    return digest(sorted(files))


def _connection(value):
    if not isinstance(value, dict) or set(value) - {"command", "args", "cwd", "env_from", "transport"}:
        raise ValueError("Поддерживается stdio: command, args, cwd, env_from. Секреты задаются ссылками на переменные окружения")
    if value.get("transport", "stdio") != "stdio":
        raise ValueError("Текущая интеграция поддерживает только stdio MCP")
    command = _local_path(value.get("command", ""))
    if not command.is_file() or command.suffix.lower() in {".cmd", ".bat", ".ps1", ".sh"}:
        raise ValueError("command должен указывать на установленный исполняемый файл, не установочный скрипт")
    args = value.get("args", [])
    env = value.get("env_from", {})
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("args должен быть массивом строк")
    if not isinstance(env, dict) or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for k, v in env.items()):
        raise ValueError("env_from: имена переменных MCP → имена переменных окружения kid")
    result = {"command": str(command), "args": args, "env_from": env}
    if value.get("cwd"):
        folder = _local_path(value["cwd"])
        if not folder.is_dir():
            raise ValueError("cwd должен быть папкой")
        result["cwd"] = str(folder)
    return result


def inspect_source(source, kind, name="", description=""):
    if kind not in TYPES:
        raise ValueError("Неизвестный тип расширения")
    path = _local_path(source)
    result = {"type": kind, "source": str(path), "name": name.strip(),
              "description": description.strip(), "version": "", "dependencies": [], "skills": [], "mcp_servers": {}}
    if kind == "skill":
        skill = _skill(path)
        result["skills"] = [skill]
        result["name"] = result["name"] or skill["name"]
        result["description"] = result["description"] or skill["description"]
        result["version"] = skill["version"]
    else:
        if path.is_dir():
            path = path / ("kid-extension.json" if kind == "plugin" else "mcp.json")
        if path.stat().st_size > 256 * 1024:
            raise ValueError("Манифест слишком большой")
        manifest = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict):
            raise ValueError("Манифест должен быть объектом JSON")
        allowed = {"name", "description", "version", "dependencies", "mcp_servers", "mcpServers"}
        allowed |= {"format", "skills"} if kind == "plugin" else {"connection"}
        if set(manifest) - allowed:
            raise ValueError("Манифест содержит неподдерживаемые поля или компоненты: " + ", ".join(sorted(set(manifest) - allowed)))
        if "mcp_servers" in manifest and "mcpServers" in manifest:
            raise ValueError("Используйте только одно поле: mcp_servers или mcpServers")
        if kind == "plugin" and manifest.get("format") != "kid-extension-v1":
            raise ValueError("Формат плагина не поддерживается. Нужен kid-extension.json с format=kid-extension-v1")
        result["name"] = result["name"] or str(manifest.get("name") or path.parent.name)
        result["description"] = result["description"] or str(manifest.get("description") or "")
        result["version"] = str(manifest.get("version") or "")
        dependencies = manifest.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ValueError("dependencies должен содержать постоянные ID библиотеки")
        result["dependencies"] = sorted(set(dependencies))
        servers = manifest.get("mcp_servers", manifest.get("mcpServers", {}))
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers должен быть объектом")
        if kind == "mcp" and not servers:
            servers = {"server": manifest.get("connection", {})}
        for key, connection in sorted(servers.items()):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", key):
                raise ValueError("Имя компонента MCP: латинские буквы, цифры, _ и -; до 40 символов")
            result["mcp_servers"][key] = _connection(connection)
        for relative in manifest.get("skills", []) if kind == "plugin" else []:
            skill_path = (path.parent / relative).resolve()
            if not skill_path.is_relative_to(path.parent.resolve()):
                raise ValueError("Скилл должен находиться внутри пакета")
            result["skills"].append(_skill(skill_path))
        if not result["skills"] and not result["mcp_servers"]:
            raise ValueError("В пакете нет поддерживаемых компонентов")
    if not result["name"] or not result["description"]:
        raise ValueError("Укажите название и короткое пользовательское описание")
    if len(result["name"]) > 120 or len(result["description"]) > 500:
        raise ValueError("Название до 120 символов, описание до 500. Полные инструкции хранятся отдельно")
    result["dependencies"] = sorted(set(result["dependencies"]) | {identity for skill in result["skills"] for identity in skill["dependencies"]})
    return result


def _copy_skill(source, target):
    """Copy support resources too, rejecting links and unbounded directories."""
    source, target = Path(source).parent, Path(target)
    files, total = [], 0
    for root, dirs, names in os.walk(source, followlinks=False):
        for name in dirs + names:
            item = Path(root) / name
            if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                raise ValueError("Ссылки и junction внутри скилла не поддерживаются")
        for name in names:
            item = Path(root) / name
            total += item.stat().st_size
            files.append(item)
            if total > 20 * 1024 * 1024 or len(files) > 1000:
                raise ValueError("Скилл превышает лимит: 20 МБ или 1000 файлов")
    for item in files:
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
    return str(target / "SKILL.md")


def install(root, cfg, preview):
    library = cfg.setdefault("extensions", [])
    if any(item["source"] == preview["source"] for item in library):
        raise ValueError("Этот источник уже есть в библиотеке")
    item = copy.deepcopy(preview)
    item["id"] = uuid.uuid4().hex
    destination = Path(root) / "extensions" / item["id"]
    try:
        for index, skill in enumerate(item["skills"]):
            skill["path"] = _copy_skill(skill["path"], destination / str(index))
            if _skill_fingerprint(Path(skill["path"]).parent) != skill["content_hash"]:
                raise ValueError("Источник изменился во время добавления. Повторите предварительный просмотр")
            skill["id"] = item["id"] + "_" + str(index)
            if item["type"] == "skill":
                skill.update({"name": item["name"], "description": item["description"]})
            elif not skill.get("description") or len(skill["description"]) > 500:
                skill["description"] = item["description"]
        item["probe"] = {}
        library.append(item)
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    return item


def states(cfg, enabled=None, provider="codex"):
    library = sorted(cfg.get("extensions", []), key=lambda item: item["id"])
    known = {item["id"]: item for item in library}
    chosen = set(known) if enabled is None else set(enabled)
    errors = {}
    values = secret_values(cfg)
    for item in library:
        identity = item["id"]
        reasons = []
        if provider != "codex":
            reasons.append("Этот провайдер пока не поддерживает расширения")
        for dependency in item.get("dependencies", []):
            if dependency not in known:
                reasons.append("Отсутствует зависимость: " + dependency)
            elif dependency not in chosen:
                reasons.append("Включите зависимость: " + known[dependency]["name"])
        for skill in item.get("skills", []):
            for dependency in skill.get("declared_tools", []):
                if dependency.get("type") != "mcp":
                    reasons.append("Тип объявленной зависимости не поддерживается: " + str(dependency.get("type")))
                    continue
                component = dependency["value"]
                candidates = [entry for entry in library if component in entry.get("mcp_servers", {})]
                if not candidates:
                    reasons.append("Отсутствует объявленная MCP-зависимость: " + component)
                elif not any(entry["id"] in chosen for entry in candidates):
                    reasons.append("Включите MCP-зависимость: " + component)
                elif not any(entry["id"] in chosen and entry.get("probe", {}).get(component, {}).get("status") == "ready" for entry in candidates):
                    reasons.append("MCP-зависимость не готова: " + component)
        for skill in item.get("skills", []):
            if not Path(skill["path"]).is_file():
                reasons.append("Файл скилла недоступен: " + skill["name"])
        for component, connection in item.get("mcp_servers", {}).items():
            probe = item.get("probe", {}).get(component, {})
            if not Path(connection["command"]).is_file():
                reasons.append("Исполняемый файл MCP недоступен: " + component)
            elif any(reference not in values for reference in connection.get("env_from", {}).values()):
                reasons.append("Задайте переменные окружения MCP: " + component)
            elif probe.get("status") != "ready":
                reasons.append(probe.get("error") or "Проверьте подключение MCP: " + component)
        if reasons:
            errors[identity] = reasons
    # Cycles and dependencies with their own failures are explicit failures too.
    visited = set()
    def visit(identity, stack):
        if identity in stack:
            for member in stack[stack.index(identity):]:
                errors.setdefault(member, []).append("Циклическая зависимость")
            return
        if identity in visited:
            return
        for dependency in known[identity].get("dependencies", []):
            if dependency in known and dependency in chosen:
                visit(dependency, stack + [identity])
        visited.add(identity)
    for identity in chosen & set(known):
        visit(identity, [])
    for _ in library:
        for identity in chosen & set(known):
            for dependency in known[identity].get("dependencies", []):
                if dependency in errors and identity not in errors:
                    errors[identity] = ["Зависимость не готова: " + known[dependency]["name"]]
            for skill in known[identity].get("skills", []):
                for dependency in skill.get("declared_tools", []):
                    if dependency.get("type") == "mcp" and not any(entry["id"] in chosen and entry["id"] not in errors
                            and dependency["value"] in entry.get("mcp_servers", {}) for entry in library):
                        errors.setdefault(identity, []).append("MCP-зависимость не готова: " + dependency["value"])
    # Identical components share one registration; different definitions cannot overwrite it.
    for item in library:
        if item["id"] not in chosen:
            continue
        for name, connection in item.get("mcp_servers", {}).items():
            # Equal launch specifications are the same component even across packages.
            tools = item.get("probe", {}).get(name, {}).get("tools", [])
            if len(tools) != len(set(tools)):
                errors.setdefault(item["id"], []).append("Конфликт имён инструментов MCP: " + name)
    result = []
    for item in library:
        reasons = list(dict.fromkeys(errors.get(item["id"], [])))
        failed = any(probe.get("status") == "error" for probe in item.get("probe", {}).values())
        result.append({**copy.deepcopy(item), "status": ("error" if failed else "requires_setup") if reasons else "ready",
                       "reasons": reasons, "selected": item["id"] in chosen,
                       "dependency_options": sorted({entry["id"] for skill in item.get("skills", [])
                           for dep in skill.get("declared_tools", []) for entry in library
                           if dep.get("type") == "mcp" and dep["value"] in entry.get("mcp_servers", {})})})
    return result


def probe(item, values=None):
    result = {}
    for name, connection in sorted(item.get("mcp_servers", {}).items()):
        client = None
        try:
            client = MCPClient(connection, secret_values=values)
            tools, cursor = [], None
            for _ in range(50):
                page = client.request("tools/list", {"cursor": cursor} if cursor else {})
                for tool in page.get("tools", []):
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                        raise ValueError("MCP вернул некорректную схему инструмента")
                    tools.append(tool["name"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            else:
                raise ValueError("Слишком много страниц инструментов MCP")
            if len(tools) != len(set(tools)):
                raise ValueError("MCP объявил инструменты с одинаковыми именами")
            result[name] = {"status": "ready", "tools": sorted(tools), "server_info": {
                key: str(client.info.get(key) or "")[:200] for key in ("name", "version")}}
        except Exception as error:
            # Never echo stderr, arguments, server error payloads or environment values.
            result[name] = {"status": "error", "error": str(error) if isinstance(error, ValueError) else "Не удалось запустить MCP"}
        finally:
            if client:
                client.close()
    return result


def snapshot(root, profile_id, fallback, previous="", refresh=True):
    with LOCK:
        return _snapshot(root, profile_id, fallback, previous, refresh)


def _snapshot(root, profile_id, fallback, previous="", refresh=True):
    root = Path(root)
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    profile = cfg.get("profiles", {}).get(profile_id, fallback)
    enabled = profile.get("extension_ids", [])
    if previous:
        path = Path(previous).resolve()
        if not path.is_relative_to((root / "extension_sessions").resolve()):
            raise ValueError("Некорректный снимок расширений")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not refresh or (saved.get("selection") == sorted(enabled) and saved.get("library") == catalog_revision(cfg, enabled)):
            return str(path), saved
    selected = [item for item in states(cfg, enabled) if item["id"] in enabled and item["status"] == "ready"]
    value = {"skills": [], "mcp_servers": {}, "selection": sorted(enabled), "library": catalog_revision(cfg, enabled), "errors": []}
    destination = root / "extension_sessions" / uuid.uuid4().hex
    destination.mkdir(parents=True)
    prepared = {}
    for item in selected:
        try:
            prepared[item["id"]] = [{**skill, "path": _copy_skill(skill["path"], destination / skill["id"])}
                                    for skill in item.get("skills", [])]
        except (ValueError, OSError):
            value["errors"].append({"id": item["id"], "error": "Не удалось подготовить файлы расширения"})
    available = set(prepared)
    # A preparation error also disables explicitly dependent components.
    for _ in selected:
        ready = {item["id"] for item in states(cfg, sorted(available)) if item["id"] in available and item["status"] == "ready"}
        if ready == available:
            break
        for identity in available - ready:
            value["errors"].append({"id": identity, "error": "Не удалось подготовить зависимость расширения"})
        available = ready
    components = set()
    for item in selected:
        if item["id"] not in available:
            continue
        for skill in prepared[item["id"]]:
            key = ("skill", skill.get("origin", skill["path"]), skill["content_hash"])
            if key not in components:
                components.add(key)
                value["skills"].append(skill)
        for name, connection in sorted(item.get("mcp_servers", {}).items()):
            key = ("mcp", digest(connection))
            if key not in components:
                components.add(key)
                value["mcp_servers"]["ext_" + item["id"] + "_" + name] = {
                    **connection, "enabled_tools": sorted(item["probe"][name]["tools"])}
    path = destination / "snapshot.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path), value


def attach(arguments, snapshot_path, value, values=None):
    values = values if values is not None else os.environ
    servers = arguments.setdefault("config", {}).setdefault("mcp_servers", {})
    errors = []
    for name, connection in sorted(value["mcp_servers"].items()):
        if name in servers:
            errors.append({"id": name.split("_", 2)[1], "error": "Конфликт регистрации MCP: " + name})
            continue
        servers[name] = {key: val for key, val in connection.items() if key != "env_from"}
        servers[name].update({"env": {key: values[reference] for key, reference in connection.get("env_from", {}).items() if reference in values},
            "enabled": True, "required": False, "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 15, "tool_timeout_sec": 60})
    skills_available = bool(value["skills"])
    if skills_available and "kid_skills" in servers:
        errors.extend({"id": skill["id"].split("_", 1)[0], "error": "Конфликт регистрации MCP: kid_skills"} for skill in value["skills"])
        skills_available = False
    if skills_available:
        servers["kid_skills"] = {"command": sys.executable,
            "args": ["-I", str(Path(__file__).resolve().parents[1] / "extensions_mcp.py"), snapshot_path],
            "enabled": True, "required": False, "default_tools_approval_mode": "approve"}
        catalog = [{"id": item["id"], "name": item["name"], "description": item["description"]} for item in value["skills"]]
        arguments["developerInstructions"] = arguments.get("developerInstructions", "") + (
            "\n\nEnabled kid skills (catalog only). When a skill is relevant, call kid_skills.read_skill with its id "
            "to read full instructions before using it. Its resources are beside the returned instruction file.\n" + json.dumps(catalog, ensure_ascii=False))
    if errors:
        value["errors"] = list({(item["id"], item["error"]): item for item in value.get("errors", []) + errors}.values())
        Path(snapshot_path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return arguments
