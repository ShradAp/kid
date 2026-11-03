"""Explicit, two-step skill imports. Downloads files; never runs package commands."""
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from services import extensions

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 1000


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ("api.github.com", "raw.githubusercontent.com"):
            raise ValueError("Источник перенаправляет загрузку на неподдерживаемый адрес")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "kid-skill-import", "Accept": "application/vnd.github+json"})
    try:
        opener = urllib.request.build_opener(_Redirects())
        with opener.open(request, timeout=25) as response:
            value = response.read(limit + 1)
    except (urllib.error.URLError, OSError):
        raise ValueError("Не удалось загрузить источник GitHub. Проверьте адрес и подключение") from None
    if len(value) > limit:
        raise ValueError("Источник превышает допустимый размер загрузки")
    return value


def _json(url):
    try:
        return json.loads(_download(url, 8 * 1024 * 1024))
    except (ValueError, UnicodeError):
        raise ValueError("GitHub не вернул корректные метаданные источника") from None


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError("Добавление скилла отменено")


def _relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or ":" in value or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Недопустимый путь внутри источника")
    return path


def _github(source):
    shorthand = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)", source)
    if shorthand:
        return (*shorthand.groups()[:2], "HEAD", "", shorthand.group(3))
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("Укажите HTTPS-ссылку GitHub/skills.sh или абсолютный локальный путь")
    parts = urllib.parse.unquote(parsed.path).strip("/").split("/")
    host = (parsed.hostname or "").lower()
    if host in ("skills.sh", "www.skills.sh") and len(parts) == 3:
        owner, repo, slug = parts
        result = (owner, repo, "HEAD", "", slug)
    elif host == "github.com" and len(parts) >= 2:
        owner, repo = parts[:2]
        repo = repo.removesuffix(".git")
        if len(parts) == 2:
            result = (owner, repo, "HEAD", "", "")
        elif len(parts) >= 5 and parts[2] in ("tree", "blob"):
            folder = "/".join(parts[4:])
            if folder.endswith("/SKILL.md"):
                folder = folder.removesuffix("/SKILL.md")
            elif folder == "SKILL.md":
                folder = "."
            elif parts[2] == "blob":
                raise ValueError("Ссылка на файл должна указывать на SKILL.md")
            result = (owner, repo, parts[3], folder, "")
        else:
            raise ValueError("Нужна ссылка на репозиторий или папку конкретного скилла GitHub")
    elif host == "raw.githubusercontent.com" and len(parts) >= 4 and parts[-1] == "SKILL.md":
        result = (parts[0], parts[1], parts[2], "/".join(parts[3:-1]) or ".", "")
    else:
        raise ValueError("Поддерживаются локальные скиллы и ссылки GitHub/skills.sh")
    owner, repo, ref, folder, slug = result
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Некорректное имя репозитория GitHub")
    if folder and folder != ".":
        _relative(folder)
    return result


def _fetch(source, target, cancel):
    owner, repo, ref, folder, slug = _github(source)
    api = "https://api.github.com/repos/" + owner + "/" + repo
    commit = _json(api + "/commits/" + urllib.parse.quote(ref, safe=""))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("GitHub вернул некорректную версию источника")
    _cancelled(cancel)
    tree = _json(api + "/git/trees/" + commit + "?recursive=1")
    if tree.get("truncated"):
        raise ValueError("Репозиторий слишком большой для импорта. Используйте локальную папку скилла")
    candidates = [entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"
                  and PurePosixPath(entry["path"]).name == "SKILL.md"]
    for path in candidates:
        _relative(path)
    if slug:
        candidates = [path for path in candidates if PurePosixPath(path).parent.name == slug]
    elif folder:
        candidates = [path for path in candidates if path == ("SKILL.md" if folder == "." else folder + "/SKILL.md")]
    if len(candidates) != 1:
        choices = ", ".join(candidates[:12])
        raise ValueError("Уточните папку конкретного скилла: " + (choices or "SKILL.md не найден"))
    instruction = candidates[0]
    folder = str(PurePosixPath(instruction).parent)
    prefix = "" if folder == "." else folder + "/"
    files = []
    total = 0
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        if not path.startswith(prefix) or entry.get("type") == "tree":
            continue
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise ValueError("Ссылки и подмодули внутри скилла не поддерживаются")
        relative = path.removeprefix(prefix)
        _relative(relative)
        total += int(entry.get("size", 0))
        files.append((relative, path))
    if len(files) > MAX_FILES or total > MAX_BYTES:
        raise ValueError("Скилл превышает лимит: 20 МБ или 1000 файлов")
    total = 0
    for relative, path in sorted(files):
        _cancelled(cancel)
        url = "https://raw.githubusercontent.com/" + owner + "/" + repo + "/" + commit + "/" + urllib.parse.quote(path, safe="/")
        content = _download(url, MAX_BYTES - total)
        total += len(content)
        destination = target.joinpath(*_relative(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    origin = "https://github.com/" + owner + "/" + repo + "/tree/" + commit + ("/" + folder if folder != "." else "")
    return {"url": source, "repository": "https://github.com/" + owner + "/" + repo, "commit": commit,
            "source_identity": origin, "instruction_url": "https://raw.githubusercontent.com/" + owner + "/" + repo + "/" + commit + "/" + instruction}


def _existing(cfg, preview, provenance):
    for item in cfg.get("extensions", []):
        if item.get("type") != "skill":
            continue
        if item.get("source") == preview["source"] or (provenance and item.get("provenance", {}).get("source_identity") == provenance["source_identity"]):
            return item
        if provenance:
            for skill in item.get("skills", []):
                marker = Path(skill["path"]).parent / "SOURCE.json"
                try:
                    legacy = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if legacy.get("url") == provenance["instruction_url"]:
                    return item
    return None


def _result(item, cfg, installed=False):
    status = next(entry for entry in extensions.states(cfg) if entry["id"] == item["id"])
    return {"id": item["id"], "skill_ids": [skill["id"] for skill in item.get("skills", [])],
            "name": item["name"], "source": item["source"], "status": status["status"],
            "reasons": status["reasons"], "installed": installed, "already_installed": not installed,
            "profile_selection_changed": False, "next_step": "Включите скилл галочкой в настройках нужного профиля"}


def _cleanup(imports, staging):
    target = Path(staging).resolve()
    if target.parent == imports.resolve() and re.fullmatch(r"[0-9a-f]{32}", target.name) and target.exists():
        shutil.rmtree(target)


def install_skill(root, load_config, save_config, *, action, source, name="", description="", fingerprint="", cancel=None):
    root = Path(root)
    imports = root / "extension_imports"
    _cancelled(cancel)
    if action == "inspect":
        staging = imports / uuid.uuid4().hex
        staging.mkdir(parents=True)
        provenance = {}
        try:
            if Path(source).is_absolute() and not source.startswith(("https:", "http:")):
                source_path = source
            else:
                provenance = _fetch(source, staging / "source", cancel)
                source_path = str(staging / "source")
            preview = extensions.inspect_source(source_path, "skill", name, description)
            folder = Path(preview["skills"][0]["path"]).parent
            files = sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())
            record = {"source": source, "preview": preview, "provenance": provenance, "created": time.time(), "staging": str(staging)}
            identity = extensions.digest(record)
            _cancelled(cancel)
            with extensions.LOCK:
                cfg = load_config()
                existing = _existing(cfg, preview, provenance)
                if existing:
                    _cleanup(imports, staging)
                    return _result(existing, cfg)
                (imports / (identity + ".json")).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            return {"name": preview["name"], "description": preview["description"], "source": provenance.get("source_identity", preview["source"]),
                    "fingerprint": identity, "files": files, "dependencies": preview["dependencies"],
                    "version": provenance.get("commit", preview.get("version", "")), "installed": False,
                    "next_step": "Сообщите пользователю источник и состав, затем вызовите install_skill с action=install и этим fingerprint"}
        except BaseException:
            _cleanup(imports, staging)
            raise
    if action != "install" or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Сначала вызовите install_skill с action=inspect, затем передайте полученный fingerprint")
    record_path = imports / (fingerprint + ".json")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Предпросмотр недоступен. Повторите inspect") from None
    if record["source"] != source or time.time() - record["created"] > 86400 or extensions.digest(record) != fingerprint:
        raise ValueError("Предпросмотр устарел или не соответствует источнику. Повторите inspect")
    expected = record["preview"]
    with extensions.LOCK:
        cfg = load_config()
        existing = _existing(cfg, expected, record["provenance"])
        if existing:
            _cleanup(imports, record["staging"])
            return _result(existing, cfg)
    preview = extensions.inspect_source(expected["source"], "skill", expected["name"], expected["description"])
    if preview != expected:
        raise ValueError("Состав скилла изменился. Повторите inspect")
    _cancelled(cancel)
    with extensions.LOCK:
        cfg = load_config()
        existing = _existing(cfg, preview, record["provenance"])
        if existing:
            return _result(existing, cfg)
        item = extensions.install(root, cfg, preview)
        if record["provenance"]:
            item["provenance"] = record["provenance"]
            item["source"] = record["provenance"]["source_identity"]
            item["version"] = item.get("version") or record["provenance"]["commit"][:12]
            item["skills"][0]["origin"] = record["provenance"]["instruction_url"]
        save_config(cfg)
        _cleanup(imports, record["staging"])
        return _result(item, cfg, installed=True)
