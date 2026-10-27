"""Profile-scoped App Server sessions, approvals and persisted turn bindings."""
from __future__ import annotations

import collections
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

from codex_engine import (CodexBlocked, LimitsCache, PROXY_BLOCKED, ProtocolError,
                          StdioConnection, profile_home, settings)
from codex_proxy import ProxyEndpoint, ProxyUnavailable, probe_exit
from codex_runtime import IsolatedRuntime, IsolationUnavailable, runtime_for
from tool_execution import ExecutionJournal, ToolExecutionError, execute_tool


def redact(text, secrets=()):
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[скрыто]")
    value = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[скрыто]@", value)
    value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [скрыто]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[токен скрыт]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[ключ скрыт]", value)
    return value


class ApprovalQueue:
    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()

    def pending(self):
        with self.lock:
            return [copy.deepcopy(item["public"]) for item in self.entries.values() if not item["event"].is_set()]

    def decide(self, identity, allow):
        with self.lock:
            item = self.entries.get(identity)
            if item is None or item["event"].is_set():
                return False
            item["allowed"] = bool(allow)
            item["event"].set()
            return True

    def wait(self, profile_id, session_id, method, params, stopped, sanitize):
        if params.get("grantRoot") and str(params["grantRoot"]) != "/workspace" and not str(params["grantRoot"]).startswith("/workspace/"):
            return {"decision": "decline"}
        identity = "codex_" + uuid.uuid4().hex
        public = {"id": identity, "operation": "Codex · " + ("команда" if "commandExecution" in method else "изменение файлов"),
                  "command": sanitize(params.get("command") or params.get("itemId") or "Изменение файлов"),
                  "description": sanitize(params.get("reason") or "Действие внутри изолированного проекта"),
                  "consequences": "Разрешение действует однократно. Сетевую изоляцию оно не снимает.",
                  "status": "pending", "profile_id": profile_id, "session_id": session_id}
        item = {"public": public, "event": threading.Event(), "allowed": False}
        with self.lock:
            self.entries[identity] = item
        deadline = time.monotonic() + 180
        try:
            while not stopped.is_set() and time.monotonic() < deadline:
                if item["event"].wait(.2):
                    return {"decision": "accept" if item["allowed"] else "decline"}
            return {"decision": "cancel"}
        finally:
            with self.lock:
                self.entries.pop(identity, None)


class SessionBindings:
    """Host-only journal; never mounted in a model-accessible directory."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.epochs = collections.defaultdict(int)

    def epoch(self, session_id):
        with self.lock:
            return self.epochs[session_id]

    def path(self, session_id):
        key = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()
        return self.directory / (key + ".json")

    def read(self, session_id):
        with self.lock:
            try:
                value = json.loads(self.path(session_id).read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                return {}

    def write(self, session_id, value, epoch=None):
        with self.lock:
            if epoch is not None and epoch != self.epochs[session_id]:
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.path(session_id)
            temporary = path.with_suffix(".tmp")
            # Retain ALL operation IDs, not just the most recent turn. A late
            # HTTP retry must never replay an older completed native command.
            operations = self.read(session_id).get("operations", {})
            if not isinstance(operations, dict):
                operations = {}
            if value.get("operation_id"):
                operations[value["operation_id"]] = {key: item for key, item in value.items() if key != "operations"}
            saved = {**value, "operations": operations}
            temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)

    def forget(self, session_id):
        with self.lock:
            self.epochs[session_id] += 1
            self.path(session_id).unlink(missing_ok=True)


class ProfileSession:
    def __init__(self, profile_id, profile, directory, limits, approvals):
        self.profile_id, self.profile = profile_id, copy.deepcopy(profile)
        self.directory, self.options = Path(directory), settings(profile)
        self.home = profile_home(directory, profile_id)
        self.bindings = SessionBindings(self.home / "bindings")
        self.endpoint = ProxyEndpoint.parse(self.options["proxy"], self.options["proxy_password"])
        self.runtime = runtime_for(directory, self.home, self.endpoint, self.options.get("workspace") or None)
        self.limits, self.approvals = limits, approvals
        self.connection = None
        self.start_lock = threading.RLock()
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.models, self.account, self.login = [], None, None
        self.limits_error = True
        self.exit_ip = None
        self.active = {}
        self.events = collections.defaultdict(lambda: collections.deque(maxlen=500))
        self.session_locks = collections.defaultdict(threading.Lock)
        self.journal = ExecutionJournal(self.home / "tool_actions")
        self.local_tools = None
        self.history_reader = None
        self.private_values = []
        try:
            configuration = json.loads((self.directory / "config.json").read_text(encoding="utf-8"))
            self.private_values.extend(configuration.get(key, "") for key in ("search_api_key", "proxy_pass", "proxy_user"))
            for item in (configuration.get("profiles") or {}).values():
                self.private_values.append(item.get("api_key", ""))
                self.private_values.append((item.get("codex") or {}).get("proxy_password", ""))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.runtime.on_tool = self.tool_bridge

    def sanitize(self, value):
        private = [self.endpoint.password, self.endpoint.username, getattr(self.runtime, "token", ""), *self.private_values]
        try:
            auth_folder = "native_server" if getattr(self.runtime, "native", False) else "server"
            auth = json.loads((self.home / auth_folder / "auth.json").read_text(encoding="utf-8"))
            for field in ("tokens",):
                private.extend(str(item) for item in (auth.get(field) or {}).values() if isinstance(item, str))
        except (OSError, ValueError, TypeError):
            pass
        return redact(value, private)

    def start(self):
        with self.start_lock:
            if self.connection is not None and not self.connection.closed and not self.runtime.stopped.is_set():
                return
            if self.closed.is_set():
                raise CodexBlocked("Сеанс остановлен; повторите подключение через тот же прокси")
            try:
                self.runtime.start_worker()
                # Socket pinning is real enforcement on the host side; the
                # kernel separately enforces no direct network in the worker.
                self.exit_ip = probe_exit(self.endpoint)["exit_ip"]
                self.runtime.start_server(self.options["search"])
                self.connection = StdioConnection(self.runtime.reader, self.runtime.writer, self.notify, self.server_request)
                self.connection.initialize()
                self.account = self.rpc("account/read", {"refreshToken": False}).get("account")
                cursor = None
                models = []
                for page in range(20):
                    result = self.rpc("model/list", {"cursor": cursor} if cursor else {})
                    models.extend(result.get("data", []))
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                self.models = models
                if self.authenticated:
                    self.refresh_limits()
                threading.Thread(target=self._poll_limits, daemon=True).start()
            except Exception as error:
                self.close()
                if isinstance(error, IsolationUnavailable):
                    raise CodexBlocked(str(error)) from None
                raise CodexBlocked("Изолированное подключение не установлено") from None

    @property
    def authenticated(self):
        return isinstance(self.account, dict) and self.account.get("type") in ("chatgpt", "chatgptAuthTokens")

    def rpc(self, method, params=None, timeout=30):
        if self.connection is None or self.runtime.stopped.is_set():
            raise CodexBlocked()
        try:
            return self.connection.request(method, params, timeout=timeout)
        except ProtocolError:
            if self.runtime.stopped.is_set() or self.runtime.failure:
                raise CodexBlocked("Потеряно соединение с прокси; прямой маршрут запрещён") from None
            raise

    def refresh_limits(self):
        try:
            if not self.authenticated:
                return
            self.limits.accept(self.profile_id, self.rpc("account/rateLimits/read"))
            self.limits_error = False
        except Exception:
            self.limits_error = True

    def _poll_limits(self):
        while not self.closed.wait(60):
            if self.runtime.stopped.is_set():
                return
            self.refresh_limits()

    def _agent_message(self, active, item_id, *, delta=None, text=None, phase=None, completed=False):
        messages = active.setdefault("agent_messages", {})
        key = item_id or "legacy-message"
        if key not in messages and messages:
            previous = next(reversed(messages.values()))
            # Older servers may omit phase: an earlier message followed by a
            # new message is progress, while the last message is the answer.
            if previous["phase"] is None and previous["text"]:
                previous["phase"] = "commentary"
                self._record_commentary(active, previous)
        item = messages.setdefault(key, {"text":"", "phase":None, "recorded":False})
        if phase is not None:
            item["phase"] = phase
        if text is not None:
            item["text"] = str(text)
        elif delta is not None:
            item["text"] += str(delta)
        if len(messages) > 2000 or sum(len(value["text"]) for value in messages.values()) > 2 * 1024 * 1024:
            active["cancel"].set()
        if completed and item["phase"] == "commentary":
            self._record_commentary(active, item)
        final = [value["text"] for value in messages.values() if value["phase"] in ("final_answer", "partial_answer")]
        if not final:
            final = [value["text"] for value in messages.values() if value["phase"] != "commentary"][-1:]
        reply = "\n\n".join(value for value in final if value)
        changed = reply != "".join(active["text"])
        active["text"][:] = [reply]
        if changed and active.get("on_text"):
            active["on_text"](self.sanitize(reply))

    def _record_commentary(self, active, item):
        if item["recorded"] or not item["text"].strip():
            return
        item["recorded"] = True
        step = self.sanitize(item["text"])[:8000]
        active["steps"].append(step)
        if active.get("on_step"):
            active["on_step"](step)

    def notify(self, method, params):
        if method == "account/rateLimits/updated":
            self.limits.accept(self.profile_id, params)
            self.limits_error = False
        elif method == "account/updated":
            mode = params.get("authMode")
            self.account = {"type": mode, "planType": params.get("planType")} if mode else None
        elif method == "account/login/completed":
            self.login = None
            if params.get("success"):
                threading.Thread(target=self._after_login, daemon=True).start()
        thread_id = params.get("threadId")
        with self.lock:
            active = self.active.get(thread_id)
            if active is None:
                return
            if method == "item/agentMessage/delta":
                self._agent_message(active, params.get("itemId"), delta=params.get("delta") or "")
            elif method == "item/reasoning/summaryTextDelta":
                active["reasoning"].append(str(params.get("delta") or ""))
            elif method == "turn/completed":
                active["turn"] = params.get("turn", {})
                active["done"].set()
            elif method in ("item/started", "item/completed"):
                item = params.get("item") or {}
                item_type = item.get("type")
                if method == "item/started" and item_type in ("commandExecution", "fileChange", "webSearch", "mcpToolCall", "dynamicToolCall"):
                    active["item_count"] += 1
                    if active["item_count"] > max(1, min(200, int(self.profile.get("max_steps", 60)))):
                        active["cancel"].set()
                text = None
                if item_type == "commandExecution":
                    text = "Команда: " + str(item.get("command") or "")
                    if method == "item/completed":
                        text += "\n" + str(item.get("aggregatedOutput") or "")
                elif item_type == "fileChange":
                    text = "Изменения файлов: " + ", ".join(str(change.get("path", "")) for change in item.get("changes", []))
                elif item_type == "webSearch":
                    text = "Поиск Codex: " + str(item.get("query") or item.get("action") or "")
                elif item_type in ("mcpToolCall", "dynamicToolCall"):
                    text = "Инструмент kid: " + str(item.get("tool") or "")
                if text:
                    safe = self.sanitize(text)[:8000]
                    active["steps"].append(safe)
                    if active.get("on_step"):
                        active["on_step"](safe)
                    self.events[active["session_id"]].append({"method": method, "type": item_type, "text": safe})
                if item_type == "agentMessage":
                    self._agent_message(active, item.get("id"), phase=item.get("phase"),
                                        text=item.get("text") if method == "item/completed" else None,
                                        completed=method == "item/completed")

    def _after_login(self):
        try:
            self.account = self.rpc("account/read", {"refreshToken": False}).get("account")
            self.refresh_limits()
        except Exception:
            pass

    def server_request(self, method, params):
        if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            raise ProtocolError("Возможность сервера недоступна")
        with self.lock:
            active = self.active.get(params.get("threadId"))
        if active is None:
            return {"decision": "cancel"}
        if active["cancel"].is_set():
            return {"decision": "cancel"}
        if (method == "item/commandExecution/requestApproval" and self.options.get("auto_approve_commands") is True
                and not params.get("networkApprovalContext") and not params.get("additionalPermissions")
                and (params.get("availableDecisions") is None or "accept" in params["availableDecisions"])):
            return {"decision": "accept"}
        return self.approvals.wait(self.profile_id, active["session_id"], method, params, active["cancel"], self.sanitize)

    def login_start(self):
        self.start()
        result = self.rpc("account/login/start", {"type": "chatgptDeviceCode"})
        if result.get("verificationUrl") != "https://auth.openai.com/codex/device":
            raise ProtocolError("Неожиданный адрес авторизации")
        self.login = {"verification_url": result["verificationUrl"], "user_code": result.get("userCode"), "login_id": result.get("loginId")}

    def logout(self):
        self.start()
        self.rpc("account/logout")
        self.account, self.login = None, None
        self.limits_error = True

    def run(self, session_id, prompt, operation_id, cancel, on_step=None, timeout=300, on_text=None):
        with self.lock:
            serial = self.session_locks[session_id]
        if not serial.acquire(blocking=False):
            raise ProtocolError("В этом диалоге уже выполняется ход")
        try:
            return self._run(session_id, prompt, operation_id, cancel, on_step, timeout, on_text)
        finally:
            serial.release()

    def thread_arguments(self, session_id):
        if getattr(self.runtime, "native", False):
            arguments = self.runtime.thread_context(session_id, self.options["search"])
            if self.options["model"]:
                arguments["model"] = self.options["model"]
            from code_workspace import windows_desktop
            arguments["developerInstructions"] = (
                "You are working on the user's actual Windows PC. Your cwd is a real Windows path. "
                "For local commands use kid windows_command (PowerShell or cmd), and for file creation "
                "use windows_write_file or an approved Windows command. The real Desktop is " + str(windows_desktop()) + ". "
                "Only claim success after the tool actually succeeds. Never invent Linux paths. "
                "The Codex service transport is strictly proxied; host commands and browsers use ordinary host networking.\n\n"
                + str(self.profile.get("system_prompt") or ""))
            return arguments
        arguments = {"cwd": "/workspace" if self.runtime.workspace else "/home/codex",
                     "approvalPolicy": "untrusted", "sandbox": "danger-full-access"}
        arguments["config"] = {"mcp_servers": {"kid": {
            "command": "/usr/local/bin/python", "args": ["-I", "/opt/kid/kid_mcp_server.py", "--port", str(self.runtime.tool_port),
                                                             "--session", str(session_id), "--search", self.options["search"]],
            "enabled": True, "tool_timeout_sec": 300}}}
        if self.options["model"]:
            arguments["model"] = self.options["model"]
        system = str(self.profile.get("system_prompt") or "")
        host_instructions = (
            "kid runs on the user's Windows PC. The App Server transport is inside a Linux container; "
            "that container is NOT the user's PC. For Windows commands use windows_command, and for files "
            "on the user's Desktop or chosen Windows project use windows_write_file. Never create a pretend "
            "/home/codex/Desktop or claim a container file exists on the user's Desktop. "
            "Only claim a host operation succeeded after the Windows tool returns ok=true and a real path. "
            "General Windows shell execution is blocked until OS network isolation is configured. "
            "Never substitute Linux commands for a requested Windows operation."
        )
        arguments["developerInstructions"] = host_instructions + ("\n\n" + system if system else "")
        return arguments

    def _run(self, session_id, prompt, operation_id, cancel, on_step=None, timeout=300, on_text=None):
        self.start()
        if not self.authenticated:
            raise ProtocolError("Войдите в аккаунт ChatGPT в настройках профиля")
        # Fingerprint the thread's execution context. A changed context starts
        # a new thread, rather than silently inheriting a different workspace.
        context_data = {key: self.options[key] for key in ("workspace", "search")}
        if getattr(self.runtime, "native", False):
            context_data["runtime"] = "windows-native-mcp-v1"
        context = hashlib.sha256(json.dumps(context_data, sort_keys=True).encode()).hexdigest()
        epoch = self.bindings.epoch(session_id)
        binding = self.bindings.read(session_id)
        previous = (binding.get("operations") or {}).get(operation_id, binding if binding.get("operation_id") == operation_id else {})
        if previous:
            if previous.get("status") in ("completed", "interrupted"):
                return previous.get("result", {})
            raise ProtocolError("Этот запрос уже отправлялся. Проверьте сохранённый результат; автоматический повтор запрещён")
        if binding.get("status") in ("pending", "active", "uncertain"):
            raise ProtocolError("Предыдущий ход требует восстановления. Автоматическая повторная отправка запрещена")
        migrated_history = ""
        if (getattr(self.runtime, "native", False) and binding.get("thread_id")
                and binding.get("context") != context and self.history_reader is not None):
            history = self.history_reader(session_id)
            if isinstance(history, list):
                excerpt = [{"role": item.get("role"), "content": str(item.get("content") or "")[:12000]}
                           for item in history[-24:] if isinstance(item, dict) and item.get("role") in ("user", "assistant")]
                if excerpt and excerpt[-1] == {"role": "user", "content": str(prompt)[:12000]}:
                    excerpt.pop()
                migrated_history = ("Previous kid conversation, quoted for context only; do not repeat completed actions:\n"
                    + json.dumps(excerpt, ensure_ascii=False)[-50000:] + "\n\nCurrent user request:\n")
        if binding.get("context") != context:
            binding = {}
        arguments = self.thread_arguments(session_id)
        if binding.get("thread_id"):
            response = self.rpc("thread/resume", {**arguments, "threadId": binding["thread_id"]})
        else:
            response = self.rpc("thread/start", arguments)
        thread_id = response["thread"]["id"]
        started_at = time.monotonic()
        active = {"session_id": session_id, "cancel": cancel, "done": threading.Event(), "text": [],
                  "reasoning": [], "steps": [], "turn": {}, "on_step": on_step, "on_text": on_text, "item_count": 0,
                  "operation_id": operation_id, "model_calls": 0, "refresh_notes": False}
        with self.lock:
            if thread_id in self.active:
                raise ProtocolError("В этом диалоге уже выполняется ход")
            self.active[thread_id] = active
        binding = {"thread_id": thread_id, "context": context, "operation_id": operation_id, "status": "pending", "prompt": str(prompt)}
        self.bindings.write(session_id, binding, epoch)
        try:
            parameters = {"threadId": thread_id, "clientUserMessageId": operation_id,
                          "input": [{"type": "text", "text": migrated_history + str(prompt)}]}
            if self.options["effort"]:
                model = next((item for item in self.models if item.get("model") == self.options["model"]), {})
                allowed = {item.get("reasoningEffort") for item in model.get("supportedReasoningEfforts", [])}
                if self.options["effort"] not in allowed:
                    raise ProtocolError("Выбранный уровень рассуждений не поддерживается моделью")
                parameters["effort"] = self.options["effort"]
            response = self.rpc("turn/start", parameters)
            turn_id = response["turn"]["id"]
            binding.update(status="active", turn_id=turn_id)
            self.bindings.write(session_id, binding, epoch)
            deadline = time.monotonic() + max(10, min(timeout, 1800))
            while not active["done"].wait(.15):
                if cancel.is_set() or time.monotonic() >= deadline:
                    cancel.set()
                    try:
                        self.rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=3)
                    except Exception:
                        pass
                    if not active["done"].wait(3):
                        self.close()  # also kills all model subprocesses
                        raise ProtocolError("Остановка не подтверждена сервером. Исход хода неизвестен; используйте восстановление, повтор заблокирован")
                    break
                if self.runtime.stopped.is_set() or self.connection.closed:
                    raise CodexBlocked()
            status = active["turn"].get("status")
            if status not in ("completed", "interrupted"):
                if status == "failed":
                    binding["status"] = "failed"
                raise ProtocolError("Ход Codex завершился ошибкой; выполненные действия не повторяются")
            result = {"reply": self.sanitize("".join(active["text"])), "thinking": active["steps"],
                      "activity": {"duration_ms": round((time.monotonic() - started_at) * 1000), "status":status},
                      "reasoning": self.sanitize("".join(active["reasoning"])), "cancelled": status == "interrupted",
                      "operation_id": operation_id, "refresh_notes": active["refresh_notes"]}
            if result["cancelled"]:
                result["reply"] = "Генерация Codex остановлена"
            binding.update(status=status, result=result)
            self.bindings.write(session_id, binding, epoch)
            return result
        except Exception:
            cancel.set()  # Resolve every outstanding approval for this failed turn.
            if binding.get("status") != "failed":
                binding["status"] = "uncertain"
            self.bindings.write(session_id, binding, epoch)
            raise
        finally:
            with self.lock:
                self.active.pop(thread_id, None)

    def recover(self, session_id):
        with self.lock:
            serial = self.session_locks[session_id]
        if not serial.acquire(blocking=False):
            raise ProtocolError("В этом диалоге уже выполняется ход")
        try:
            return self._recover(session_id)
        finally:
            serial.release()

    def _recover(self, session_id):
        self.start()
        epoch = self.bindings.epoch(session_id)
        binding = self.bindings.read(session_id)
        if not binding.get("thread_id"):
            raise ProtocolError("У этого диалога нет сохранённой сессии Codex")
        if binding.get("status") == "completed":
            return {**binding["result"], "prompt": binding.get("prompt", "")}
        # Refresh the ephemeral relay port after every process restart.
        self.rpc("thread/resume", {**self.thread_arguments(session_id), "threadId": binding["thread_id"]})
        value = self.rpc("thread/read", {"threadId": binding["thread_id"], "includeTurns": True})
        turns = value.get("thread", {}).get("turns", [])
        turn = next((item for item in turns if item.get("id") == binding.get("turn_id")), None)
        if turn is None:
            turn = next((item for item in turns if any(
                step.get("type") == "userMessage" and step.get("id") == binding.get("operation_id")
                for step in item.get("items", []))), None)
        if turn is None or turn.get("status") not in ("completed", "interrupted", "failed"):
            raise ProtocolError("Исход хода пока неизвестен. Повторное исполнение заблокировано; проверьте позже")
        recovered = {"text":[], "steps":[], "cancel":threading.Event()}
        for index, item in enumerate(turn.get("items", [])):
            if item.get("type") == "agentMessage":
                self._agent_message(recovered, item.get("id") or str(index), text=item.get("text") or "", phase=item.get("phase"), completed=True)
        result = {"reply": self.sanitize("".join(recovered["text"])), "thinking": recovered["steps"], "reasoning": "", "cancelled": turn["status"] != "completed",
                  "activity": {"status":turn["status"]},
                  "operation_id": binding.get("operation_id")}
        binding.update(status=turn["status"], result=result)
        self.bindings.write(session_id, binding, epoch)
        return {**result, "prompt": binding.get("prompt", "")}

    def forget(self, session_id):
        with self.lock:
            for active in self.active.values():
                if active["session_id"] == session_id:
                    active["cancel"].set()
        self.bindings.forget(session_id)

    def model_completion(self, messages, timeout, cancel):
        if not isinstance(messages, list) or len(json.dumps(messages)) > 300000:
            raise ProtocolError("Превышен размер контекста исследования")
        system = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        data = [{"role": item.get("role"), "content": item.get("content")} for item in messages if item.get("role") != "system"]
        arguments = {"ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
                                          "developerInstructions": system,
                                          "config": {"web_search": "disabled", "mcp_servers": {}, "features.shell_tool": False,
                                                     "features.code_mode": False, "features.unified_exec": False, "features.code_mode_host": False}}
        if self.options["model"]:
            arguments["model"] = self.options["model"]
        thread = self.rpc("thread/start", arguments)
        thread_id = thread["thread"]["id"]
        active = {"session_id": "research", "cancel": cancel, "done": threading.Event(), "text": [], "reasoning": [],
                  "steps": [], "turn": {}, "on_step": None, "item_count": 0}
        with self.lock:
            self.active[thread_id] = active
        turn_id = None
        try:
            response = self.rpc("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]})
            turn_id = response["turn"]["id"]
            deadline = time.monotonic() + max(1, min(45, timeout))
            while not active["done"].wait(.15):
                if cancel.is_set() or time.monotonic() >= deadline or self.runtime.stopped.is_set():
                    self.rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    raise ProtocolError("Оценка источников Codex остановлена")
            if active["turn"].get("status") != "completed":
                raise ProtocolError("Codex не завершил оценку источников")
            return "".join(active["text"])
        finally:
            with self.lock:
                self.active.pop(thread_id, None)

    def tool_bridge(self, frame):
        request = frame.get("request") or {}
        session_id = str(request.get("session_id") or "")
        with self.lock:
            active = next((item for item in self.active.values() if item["session_id"] == session_id), None)
        if active is None or active["cancel"].is_set():
            return {"isError": True, "content": [{"type": "text", "text": "Сеанс инструмента не активен"}]}
        if frame.get("kind") == "model_call":
            with self.lock:
                active["model_calls"] += 1
                if active["model_calls"] > 10:
                    return {"error": "Бюджет модельных оценок исчерпан"}
            try:
                return {"text": self.model_completion(request.get("messages"), float(request.get("timeout", 20)), active["cancel"])}
            except Exception:
                return {"error": "Модельная оценка недоступна"}
        name, arguments = request.get("name"), request.get("arguments", {})
        allowed = {"local_search", "create_downloadable_file", "save_note", "list_notes", "remember_user_fact", "manage_memory", "set_system_prompt", "windows_command", "windows_write_file"}
        if self.options["search"] == "kid":
            allowed.update(("web_search", "fetch_webpage", "analyze_github_repo"))
        if name not in allowed or not isinstance(arguments, dict):
            return {"isError": True, "content": [{"type": "text", "text": "Инструмент запрещён выбранным поисковым маршрутом"}]}
        def perform():
            if active["cancel"].is_set():
                raise ToolExecutionError("Действие отменено")
            detail = arguments.get("url") or arguments.get("query") or arguments.get("command") or arguments.get("file_name") or ""
            label = "Чтение текста страницы инструментом kid" if name == "fetch_webpage" else "Поиск kid" if name == "web_search" else f"Инструмент kid: {name}"
            step = self.sanitize(f"{label}: {detail}")[:2000]
            active["steps"].append(step)
            self.events[session_id].append({"method": "kid/tool/started", "type": "kidToolCall", "text": step})
            if active.get("on_step"):
                active["on_step"](step)
            if name in ("save_note", "list_notes", "set_system_prompt", "windows_command", "windows_write_file"):
                if self.local_tools is None:
                    raise ToolExecutionError("Локальный инструмент не подключён")
                result = self.local_tools(name, arguments, self.profile_id, active["cancel"])
                if name == "save_note":
                    active["refresh_notes"] = True
                return result
            return self.isolated_tool(session_id, name, arguments, active["cancel"])
        try:
            result = self.journal.execute(active["operation_id"], name, arguments, perform)
            if name in ("save_note", "remember_user_fact", "manage_memory", "set_system_prompt", "create_downloadable_file", "windows_command", "windows_write_file"):
                detail = arguments.get("title") or arguments.get("fact") or arguments.get("action") or arguments.get("file_name") or ""
                if name == "create_downloadable_file" and isinstance(result, dict):
                    detail = result.get("download_markdown") or detail
                if name in ("windows_command", "windows_write_file") and isinstance(result, dict):
                    detail = result.get("path") or result.get("stdout") or result.get("error") or ""
                text = self.sanitize(f"Результат инструмента kid · {name}: {detail}")[:2000]
                active["steps"].append(text)
                self.events[session_id].append({"method": "kid/tool/completed", "type": "kidToolResult", "text": text})
                if active.get("on_step"):
                    active["on_step"](text)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return {"content": [{"type": "text", "text": self.sanitize(text)[:150000]}], "isError": False}
        except Exception as error:
            message = str(error) if isinstance(error, ToolExecutionError) else PROXY_BLOCKED
            return {"content": [{"type": "text", "text": message}], "isError": True}

    def isolated_tool(self, session_id, name, arguments, cancel):
        if getattr(self.runtime, "native", False):
            from native_codex.tools import execute
            def model(messages, timeout):
                response = self.tool_bridge({"kind": "model_call", "request": {
                    "session_id": session_id, "messages": messages, "timeout": timeout}})
                if "text" not in response:
                    raise ToolExecutionError("Контроллер исследования Codex недоступен")
                return response["text"]
            try:
                return execute(name, arguments, cancel, model)
            except ToolExecutionError:
                raise
            except Exception:
                raise ToolExecutionError("Инструмент kid недоступен или завершился ошибкой") from None
        # The second worker shares selected tool data, never API credentials or
        # the App Server's auth directory. Its host broker is still proxy-only.
        runtime = IsolatedRuntime(self.directory, self.home / "tools", self.endpoint, network_scope="tools", on_tool=self.tool_bridge)
        try:
            runtime.start_worker()
            config = {}
            try:
                config = json.loads((self.directory / "config.json").read_text(encoding="utf-8"))
                notes = json.loads((self.directory / "notes.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                notes = []
            request = {"session_id": session_id, "name": name, "arguments": arguments,
                       "search_api_key": config.get("search_api_key", ""),
                       "research_mode": config.get("search_research_mode", "adaptive"), "notes": notes}
            runtime.send({"kind": "execute_tool", "request": request})
            deadline = time.monotonic() + 175
            while not cancel.is_set() and time.monotonic() < deadline:
                try:
                    frame = runtime.events.get(timeout=.2)
                except __import__("queue").Empty:
                    continue
                if frame.get("kind") == "tool_executed":
                    result = frame.get("result", {})
                    if result.get("error"):
                        raise ToolExecutionError(result["error"])
                    return result.get("result")
                if frame.get("kind") == "fatal":
                    raise ProxyUnavailable()
            raise ToolExecutionError("Инструмент остановлен по отмене или бюджету времени")
        finally:
            runtime.close()

    def close(self):
        self.closed.set()
        with self.lock:
            for active in self.active.values():
                active["cancel"].set()
        if self.connection:
            self.connection.close()
        self.runtime.close()


class CodexService:
    def __init__(self, directory, limits=None, workspace_grant=None, local_tools=None):
        self.directory = Path(directory)
        self.limits = limits or LimitsCache(directory)
        self.approvals = ApprovalQueue()
        self.sessions = {}
        self.lock = threading.RLock()
        self.errors = {}
        self.workspace_grant = workspace_grant
        self.local_tools = local_tools
        self.bootstrap = None
        self.history_reader = None

    def project_diff(self, profile_id, profile, relative):
        session = self.get(profile_id, profile)
        if not session.runtime.workspace:
            raise ProtocolError("Выберите рабочую папку проекта")
        session.start()
        if getattr(session.runtime, "native", False):
            from native_codex.diff import project_diff
            result = project_diff(session.runtime.workspace, relative)
            result["text"] = session.sanitize(result["text"])
            return result
        result = json.loads(session.runtime.docker("exec", session.runtime.container, "/usr/local/bin/python", "-I",
                                                   "/opt/kid/kidlib/kid_diff_worker.py", str(relative), timeout=15))
        if result.get("error"):
            raise ProtocolError(result["error"])
        result["text"] = session.sanitize(result.get("text", ""))
        return result

    def get(self, profile_id, profile):
        selected = settings(profile).get("workspace")
        if selected:
            try:
                if self.workspace_grant is None or Path(self.workspace_grant(profile_id)).resolve() != Path(selected).resolve():
                    raise ValueError()
            except (ValueError, OSError):
                raise CodexBlocked("Повторно выберите рабочую папку системным диалогом") from None
        fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            saved = self.sessions.get(profile_id)
            if saved and saved[0] == fingerprint and not saved[1].closed.is_set() and not saved[1].runtime.stopped.is_set():
                return saved[1]
            if saved:
                saved[1].close()
            session = ProfileSession(profile_id, profile, self.directory, self.limits, self.approvals)
            session.local_tools = self.local_tools
            session.history_reader = self.history_reader
            self.sessions[profile_id] = (fingerprint, session)
            return session

    def status(self, profile_id, profile):
        try:
            if profile_id in self.errors:
                raise CodexBlocked()
            # Missing settings/image may be reported without starting a worker
            # or probing the Internet. Runtime verification happens at start.
            saved = self.sessions.get(profile_id)
            if saved and saved[1].connection is not None and (saved[1].runtime.stopped.is_set() or saved[1].connection.closed):
                self.errors[profile_id] = "Соединение закрыто. Повторите подключение через тот же прокси"
                raise CodexBlocked()
            session = self.get(profile_id, profile)
            session.runtime.image_metadata()
            alive = session.connection is not None and not session.connection.closed and not session.runtime.stopped.is_set()
            return {"engine": "codex", "available": True, "proxy_state": "verified" if alive else "not_checked",
                    "error": "", "reason": "Только изолированный запуск; прямой маршрут отсутствует",
                    "auth_state": "authenticated" if session.authenticated else "login_pending" if session.login else "not_authenticated" if alive else "not_checked",
                    "login": copy.deepcopy(session.login), "exit_ip": session.exit_ip,
                    "models": copy.deepcopy(session.models), "isolation": session.runtime.report,
                    "limits": self.limits.read(profile_id, live=alive and not session.limits_error),
                    "capabilities": {"login": True, "chat": session.authenticated,
                                     "tools": True, "search": True,
                                     "commands": True, "general_windows_commands": bool(getattr(session.runtime, "native", False)),
                                     "file_changes": bool(session.options["workspace"])}}
        except (IsolationUnavailable, ProxyUnavailable, CodexBlocked):
            from codex_engine import status
            result = status(profile_id, profile, self.limits)
            if profile_id in self.errors:
                result["reason"] = self.errors[profile_id]
            return result

    def action(self, profile_id, profile, action):
        self.errors.pop(profile_id, None)
        try:
            if self.bootstrap is not None and action != "logout":
                self.bootstrap(self.directory, profile_id, profile)
            session = self.get(profile_id, profile)
            if action == "login":
                session.login_start()
            elif action == "logout":
                session.logout()
            else:
                session.start()
                session.refresh_limits()
            return self.status(profile_id, profile)
        except Exception as error:
            self.errors[profile_id] = (error.reason if isinstance(error, CodexBlocked) else
                                       str(error) if isinstance(error, ProtocolError) else "Защищённое подключение недоступно")
            return self.status(profile_id, profile)

    def close(self):
        with self.lock:
            for _, session in self.sessions.values():
                session.close()

    def forget(self, session_id):
        with self.lock:
            for _, session in self.sessions.values():
                session.forget(session_id)
        # Also forget closed/unloaded profiles' host journals.
        for path in (self.directory / "codex").glob("*/bindings"):
            SessionBindings(path).forget(session_id)
