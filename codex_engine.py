"""Codex profile boundary and offline App Server protocol support.

A proxy URL, environment variables, a successful probe or a checkbox do not
establish containment. Windows launches require persistent WFP restrictions
and live transport checks before starting App Server. Missing protection fails
closed, including token refreshes during startup.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlsplit

PROXY_BLOCKED = "Прокси недоступен. Запрос Codex заблокирован"
ISOLATION_REASON = (
    "Не подтверждена готовность изолированной среды и прокси. "
    "Проверьте настройки прокси и подключите профиль: kid подготовит Windows-компоненты "
    "и при необходимости запросит однократное подтверждение защиты Windows. Прямой маршрут запрещён."
)
PROTOCOL_VERSION = "0.164.0-alpha.11.1"


class CodexBlocked(RuntimeError):
    def __init__(self, reason=ISOLATION_REASON):
        super().__init__(PROXY_BLOCKED)
        self.reason = reason


def engine(profile):
    value = profile.get("engine", "api")
    if value not in ("api", "codex"):
        raise ValueError("Неизвестный движок профиля")
    return value


def settings(profile):
    value = profile.get("codex", {})
    result = copy.deepcopy(value) if isinstance(value, dict) else {}
    result.setdefault("model", "")
    result.setdefault("effort", "")
    result.setdefault("search", "codex")
    result.setdefault("proxy", "")
    result.setdefault("proxy_password", "")
    result.setdefault("workspace", "")
    result["auto_approve_commands"] = result.get("auto_approve_commands") is True
    return result


def public_settings(profile):
    private = settings(profile)
    result = {key: private[key] for key in ("model", "effort", "search", "proxy", "workspace", "auto_approve_commands")}
    # Also redact a hand-edited/legacy URL that bypassed form validation.
    try:
        parsed = urlsplit(result["proxy"])
        if parsed.password is not None:
            result["proxy"] = ""
    except (TypeError, ValueError):
        result["proxy"] = ""
    result["has_proxy_password"] = bool(private.get("proxy_password"))
    return result


def update_profile(existing, persona):
    """Merge only this profile; never read/write another Codex client's home."""
    updated = copy.deepcopy(existing)
    selected = persona.get("engine", updated.get("engine", "api"))
    if selected not in ("api", "codex"):
        raise ValueError("Неизвестный движок профиля")
    updated["engine"] = selected
    incoming = persona.get("codex")
    if incoming is not None:
        if not isinstance(incoming, dict):
            raise ValueError("Некорректные настройки Codex")
        result = settings(updated)
        if "auto_approve_commands" in incoming:
            if not isinstance(incoming["auto_approve_commands"], bool):
                raise ValueError("Некорректный режим подтверждения команд")
            result["auto_approve_commands"] = incoming["auto_approve_commands"]
        for key in ("model", "effort", "search", "proxy"):
            if key in incoming:
                if not isinstance(incoming[key], str):
                    raise ValueError("Некорректные настройки Codex")
                result[key] = incoming[key].strip()
        # A workspace can only be granted by the native directory picker.
        if result["search"] not in ("codex", "kid"):
            raise ValueError("Неизвестный поисковый движок Codex")
        if result["proxy"]:
            try:
                parsed = urlsplit(result["proxy"])
                if (parsed.scheme not in ("http", "https", "socks5h") or not parsed.hostname
                        or not parsed.port or parsed.password is not None
                        or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                    raise ValueError()
            except ValueError:
                # Never include the supplied URL in an exception or a log.
                raise ValueError("Укажите прокси с портом; пароль вводится в отдельном поле") from None
        password = incoming.get("proxy_password")
        if incoming.get("clear_proxy_password") is True:
            result["proxy_password"] = ""
        elif isinstance(password, str) and password:
            result["proxy_password"] = password
        updated["codex"] = result
    return updated


def require_isolation(profile, directory=None):
    """Availability check; launch MUST additionally verify the live namespace."""
    if not settings(profile)["proxy"]:
        raise CodexBlocked("Прокси не настроен. " + ISOLATION_REASON)
    if directory is None:
        from app_paths import USER_DATA_DIR
        directory = USER_DATA_DIR
    from codex_proxy import ProxyEndpoint, ProxyUnavailable
    from codex_runtime import runtime_for, IsolationUnavailable
    try:
        options = settings(profile)
        endpoint = ProxyEndpoint.parse(options["proxy"], options["proxy_password"])
        runtime_for(directory, Path(directory) / "unused", endpoint).image_metadata()
    except (ProxyUnavailable, IsolationUnavailable):
        raise CodexBlocked() from None


def profile_home(root, profile_id):
    # Hashing avoids path traversal AND collisions from replacing punctuation.
    key = hashlib.sha256(str(profile_id).encode("utf-8")).hexdigest()
    return Path(root) / "codex" / key


class LimitsCache:
    """Only server-reported subscription windows; no inferred defaults."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()

    def _path(self, profile_id):
        return profile_home(self.directory, profile_id) / "limits.json"

    def read(self, profile_id, live=False):
        with self.lock:
            try:
                data = json.loads(self._path(profile_id).read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
                    raise ValueError()
            except (OSError, ValueError):
                data = {"windows": [], "updated_at": None}
            return {**data, "stale": not live or not data.get("updated_at") or time.time() - data["updated_at"] > 90}

    def accept(self, profile_id, payload, now=None):
        buckets = payload.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict) or not buckets:
            bucket = payload.get("rateLimits")
            if not isinstance(bucket, dict):
                raise ValueError("App Server не вернул лимиты")
            buckets = {bucket.get("limitId") or "codex": bucket}
        windows = []
        for bucket_id, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            for slot in ("primary", "secondary"):
                item = bucket.get(slot)
                if not isinstance(item, dict):
                    continue
                def number(key, lower, upper):
                    value = item.get(key)
                    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value) and lower <= value <= upper else None)
                used = number("usedPercent", 0, 100)
                duration = number("windowDurationMins", 1, 10**9)
                windows.append({"bucket": str(bucket_id)[:100], "slot": slot,
                                "duration_minutes": duration,
                                "remaining_percent": 100 - used if used is not None else None,
                                "resets_at": number("resetsAt", 0, 10**12)})
        data = {"windows": windows, "updated_at": time.time() if now is None else now}
        with self.lock:
            path = self._path(profile_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.replace(path)
        return {**data, "stale": False}


def status(profile_id, profile, cache):
    try:
        require_isolation(profile, cache.directory)
        reason = ISOLATION_REASON
    except CodexBlocked as exc:
        reason = exc.reason
    return {"engine": "codex", "available": False, "proxy_state": "blocked",
            "error": PROXY_BLOCKED, "reason": reason, "auth_state": "unavailable",
            "models": [], "limits": cache.read(profile_id),
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": {name: False for name in (
                "login", "chat", "tools", "search", "commands", "file_changes")}}


class ProtocolError(RuntimeError):
    pass


class StdioConnection:
    """JSON-line RPC over already-open streams, with no process/network launch.

    Kept separate so protocol handling can be checked offline. Production must
    supply streams ONLY from a verified isolated launcher (not present yet).
    Unknown server requests are rejected, never approved or executed implicitly.
    """
    def __init__(self, reader, writer, on_notification=None, on_request=None):
        self.reader, self.writer = reader, writer
        self.on_notification = on_notification or (lambda method, params: None)
        self.on_request = on_request
        self.server_requests = set()
        self.pending = {}
        self.serial = 0
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.closed = False
        self.worker = threading.Thread(target=self._read, daemon=True)
        self.worker.start()

    def _send(self, message):
        with self.write_lock:
            try:
                self.writer.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.writer.flush()
            except (OSError, ValueError):
                raise ProtocolError("Соединение App Server закрыто") from None

    def request(self, method, params=None, timeout=30):
        with self.lock:
            if self.closed:
                raise ProtocolError("Соединение App Server закрыто")
            self.serial += 1
            request_id = self.serial
            response = queue.Queue(maxsize=1)
            self.pending[request_id] = response
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            try:
                message = response.get(timeout=timeout)
            except queue.Empty:
                # A timeout does NOT imply the action did not execute. No replay.
                raise ProtocolError("App Server не ответил; автоматический повтор действия отключён") from None
            if "error" in message:
                # Server errors can contain credentials/URLs. Never forward them raw.
                raise ProtocolError("Ошибка App Server")
            return message.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def initialize(self):
        params = {"clientInfo": {"name": "kid", "title": "kid", "version": "0.1.0"}}
        result = self.request("initialize", params)
        self._send({"method": "initialized"})
        return result

    def _read(self):
        try:
            while not self.closed:
                # Bound each line to prevent runaway child output.
                line = self.reader.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if self.closed:
                    break
                if len(line) > 8 * 1024 * 1024:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    break
                if "method" in message:
                    if "id" in message:
                        if self.on_request is None:
                            self._send({"id": message["id"], "error": {
                                "code": -32601, "message": "Client capability unavailable"}})
                        else:
                            with self.lock:
                                if message["id"] in self.server_requests or len(self.server_requests) >= 32:
                                    raise ProtocolError("Некорректный запрос сервера")
                                self.server_requests.add(message["id"])
                            threading.Thread(target=self._answer, args=(message,), daemon=True).start()
                    else:
                        self.on_notification(message["method"], message.get("params", {}))
                else:
                    with self.lock:
                        waiter = self.pending.get(message.get("id"))
                        if waiter is not None and waiter.empty():
                            waiter.put_nowait(message)
        except Exception:
            # Do not dump child output or callback exceptions into logs.
            pass
        finally:
            self.close()

    def _answer(self, message):
        try:
            result = self.on_request(message["method"], message.get("params", {}))
            self._send({"id": message["id"], "result": result})
        except Exception:
            try:
                self._send({"id": message["id"], "error": {"code": -32601, "message": "Client capability unavailable"}})
            except ProtocolError:
                pass
        finally:
            with self.lock:
                self.server_requests.discard(message["id"])

    def close(self):
        with self.lock:
            self.closed = True
            for waiter in self.pending.values():
                if waiter.empty():
                    waiter.put_nowait({"error": {"code": -32000}})


class CodexAdapter:
    """Production entry point. Nothing may start before the isolation gate."""
    def __init__(self, profile_id, profile, directory):
        self.profile_id = profile_id
        self.profile = copy.deepcopy(profile)
        self.home = profile_home(directory, profile_id)

    def start(self):
        require_isolation(self.profile, self.home.parents[1])
        if not hasattr(self, "session"):
            from codex_service import ApprovalQueue, ProfileSession
            self.session = ProfileSession(self.profile_id, self.profile, self.home.parents[1],
                                          LimitsCache(self.home.parents[1]), ApprovalQueue())
        self.session.start()

    def request(self, method, params=None):
        self.start()
        return self.session.rpc(method, params)
