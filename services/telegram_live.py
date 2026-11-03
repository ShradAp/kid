"""Disposable previews and a reusable panel; never run network IO on Codex callbacks."""

import time
import uuid

from services.telegram_transport import TelegramError, text_chunks
import codex_engine


class TelegramLive:
    INTERVAL = 1.5

    def __init__(self, bot):
        self.bot = bot
        self.turn = None
        self.next_preview = 0
        self.next_panel = 0
        self.panel_requested = False
        self.panel_body = None
        self.panel_new = False
        self.panel_target = None
        self.panel_reply_to = None
        self.preview_attempted = set()
        self.outage = None
        self.started_notice = False
        self.clear_requested = None
        self.preview_error = ""

    def begin(self, session_id, identity):
        self.turn = {"session_id": session_id, "identity": identity,
                     "draft_id": uuid.uuid4().int % (2**31 - 1) + 1,
                     "started": time.monotonic(), "text": "", "phase": "Подготовка запроса"}
        self.next_preview = 0
        self.next_panel = 0

    def update(self, session_id, identity, text=None, phase=None):
        with self.bot.lock:
            if not self.turn or (self.turn["session_id"], self.turn["identity"]) != (session_id, identity):
                return
            if text is not None:
                # on_text supplies the complete current answer, not a delta.
                self.turn["text"] = str(text)
            if phase:
                self.turn["phase"] = phase
        self.bot.live_ready.set()

    def stop(self, draft_id):
        with self.bot.lock:
            turn = self.turn
            if (turn and str(turn["draft_id"]) == str(draft_id) and self.bot.current
                    and self.bot.current == (turn["session_id"], turn["identity"])
                    and self.bot.store.state()["session_id"] == turn["session_id"]):
                self.bot.stop_turn()
                return True
        return False

    def request_panel(self, new=True, message_id=None, reply_to=None):
        self.panel_requested = True
        self.panel_new = new
        self.panel_target = message_id
        self.panel_reply_to = reply_to
        if not new:
            self.bot.store.change(lambda saved: saved.update(panel_live=False))
        self.next_panel = 0
        self.bot.live_ready.set()

    def _finish_preview(self, record, state):
        bot = self.bot
        last = state.get("last_turn") or {}
        terminal = True
        if record["session_id"] != state["session_id"]:
            body = "Контекст очищен. Запрос остановлен."
        elif last.get("operation_id") == record["operation_id"]:
            if last.get("delivery_ms") is None:
                body, terminal = "Ответ подготовлен. Доставляю…", False
            else:
                body = {"ошибка":"Запрос завершился с ошибкой. Подробности — в ответе.",
                        "остановлен":"Запрос остановлен."}.get(last.get("outcome"), "Готово. Ответ отправлен ниже.")
        else:
            body = "Запрос завершён или прерван. Проверьте последний ответ."
        if body != record.get("body"):
            bot.client.call("editMessageText", chat_id=int(bot.settings["owner_id"]), message_id=record["message_id"],
                            text=body, reply_markup={"inline_keyboard":[]})
        def save(saved):
            current = saved.get("preview_message") or {}
            if current.get("message_id") == record["message_id"]:
                if terminal:
                    saved.pop("preview_message", None)
                else:
                    current["body"] = body
        bot.store.change(save)

    def _preview(self, turn, state, now):
        bot = self.bot
        record = state.get("preview_message")
        same = bool(turn and record and record["session_id"] == turn["session_id"] and record["identity"] == turn["identity"])
        if record and not same:
            try:
                self._finish_preview(record, state)
            except TelegramError as error:
                if not (error.not_modified or error.message_missing):
                    self.preview_error = "Не удалось обновить статус; готовый ответ доставляется отдельно"
                    return
                bot.store.change(lambda saved: saved.pop("preview_message", None))
            record = None
        if not turn or now < self.next_preview or now - turn["started"] < .4:
            return
        self.next_preview = now + self.INTERVAL
        if not (bot.settings.get("live_status", True) or bot.settings.get("streaming", True)):
            return
        text = turn["text"] if bot.settings.get("streaming", True) else ""
        heading = (turn["phase"] + f" · {int((now - turn['started']) // 10) * 10} с") if bot.settings.get("live_status", True) else "Ответ готовится…"
        # Keep a short, stable-height preview. Full rich formatting belongs only
        # to the final answer. Native ephemeral drafts caused viewport jumps on
        # the user's Android client, so use an ordinary editable message.
        snippet = next(text_chunks(text, 450)) if text else ""
        snippet = "\n".join(snippet.splitlines()[:6])
        body = heading + ("\n\n" + snippet + ("\n… Полный ответ придёт отдельно." if snippet != text else "") if snippet else "")
        if same and record.get("body") == body:
            return
        key = (turn["session_id"], turn["identity"])
        if not same and key in self.preview_attempted:
            return
        params = {"chat_id":int(bot.settings["owner_id"]), "text":body,
                  "reply_markup":{"inline_keyboard":[[{"text":"Остановить", "callback_data":f"turn:stop:{turn['draft_id']}"}]]}}
        try:
            if same:
                params["message_id"] = record["message_id"]
                bot.client.call("editMessageText", **params)
            else:
                self.preview_attempted.add(key)
                params["disable_notification"] = True
                receipt = bot.client.call("sendMessage", **params)
                record = {"session_id":turn["session_id"], "identity":turn["identity"], "message_id":receipt["message_id"],
                          "operation_id":f"telegram_{bot.settings['token'].split(':')[0]}_{turn['identity']}"}
            record["body"] = body
            def remember(saved):
                saved["preview_message"] = record
                if not same:
                    saved["panel_live"] = False
            bot.store.change(remember)
            self.preview_error = ""
        except TelegramError as error:
            self.preview_error = "Предпросмотр временно недоступен; готовый ответ будет доставлен обычным способом"
            self.next_preview = now + error.retry_after

    def panel(self, state, turn):
        bot = self.bot
        profile_id = bot.settings["profile_id"]
        profile = (bot.load_config().get("profiles") or {}).get(profile_id) or {}
        status = bot.codex.status(profile_id, profile)
        status = status if isinstance(status, dict) else {}
        proxy = {"verified": "проверен", "not_checked": "не проверен"}.get(status.get("proxy_state"), "недоступен / нет данных")
        auth = {"authenticated": "вход выполнен", "login_pending": "ожидается вход"}.get(status.get("auth_state"), "нужна проверка входа")
        phase = "Свободен"
        if turn:
            phase = turn["phase"] + f" · {int((time.monotonic() - turn['started']) // 5) * 5} с"
        elif state["outbox"]:
            phase = "Доставка ответа"
        lines = ["Панель KID", f"Профиль: {profile.get('display_name') or profile_id}",
                 f"Модель: {codex_engine.settings(profile).get('model') or status.get('model') or 'из настроек профиля'}",
                 f"Запрос: {phase}", f"В очереди: {sum(i['kind'] == 'prompt' for i in state['inbox'])} · К доставке: {len(state['outbox'])}",
                 f"Прокси Codex: {proxy}. Аккаунт: {auth}.",
                 f"Telegram: {'через прокси KID' if bot.settings['network'] == 'proxy' else 'напрямую с ПК'}"]
        if bot.error:
            lines.append("Связь: есть ошибка, KID повторяет соединение")
        else:
            lines.append("Связь с Telegram: работает")
        limits = status.get("limits") or {}
        windows = limits.get("windows") or []
        for window in windows[:4]:
            remaining = window.get("remaining_percent")
            if isinstance(remaining, (int, float)):
                lines.append(f"Лимит {window.get('duration_minutes', '?')} мин: осталось {remaining:g}%" + (" (данные устарели)" if limits.get("stale") else ""))
        if not windows:
            lines.append("Лимиты: сервер пока не сообщил данные")
        last = state.get("last_turn") or {}
        if last:
            lines.append(f"Последний запрос: {last.get('outcome', 'завершён')} · Codex {last.get('codex_ms', 0) / 1000:.1f} с")
            if last.get("total_ms") is not None:
                lines.append(f"До доставки: {last['total_ms'] / 1000:.1f} с")
        markup = {"inline_keyboard": [[{"text": "Обновить", "callback_data": "panel:refresh"}]]}
        if turn:
            markup["inline_keyboard"][0].append({"text": "Остановить", "callback_data": f"panel:stop:{turn['draft_id']}"})
        if self.clear_requested == state["session_id"]:
            lines.append("Очистить контекст, историю и очередь KID? Сообщения в Telegram останутся.")
            markup["inline_keyboard"].append([
                {"text": "Да, очистить", "callback_data": "panel:confirm:" + state["session_id"].removeprefix("telegram_")},
                {"text": "Отмена", "callback_data": "panel:cancel"}])
        else:
            markup["inline_keyboard"].append([{"text": "Очистить чат", "callback_data": "panel:clear"}])
        return "\n".join(lines), markup

    def tick(self):
        bot = self.bot
        now = time.monotonic()
        with bot.lock:
            state = bot.store.state()
            turn = dict(self.turn) if (self.turn and bot.current == (self.turn["session_id"], self.turn["identity"])
                                       and state["session_id"] == self.turn["session_id"]) else None
            if turn:
                if bot.cancel is not None and bot.cancel.is_set():
                    turn["phase"] = "Останавливаю запрос"
                elif any(entry.get("session_id") == turn["session_id"]
                         and entry.get("profile_id") == bot.settings["profile_id"]
                         for entry in bot.codex.approvals.pending()):
                    turn["phase"] = "Ожидаю вашего разрешения"
                elif any(entry.get("kind") == "document" for entry in state["outbox"]):
                    turn["phase"] = "Отправляю файл"
        self._preview(turn, state, now)
        panel_id = (self.panel_target or state.get("panel_message_id")) if not self.panel_new else None
        if (self.panel_requested or (panel_id and state.get("panel_live"))) and now >= self.next_panel:
            self.next_panel = now + (5 if turn else 30)
            body, markup = self.panel(state, turn)
            signature = (body, str(markup))
            if signature != self.panel_body or self.panel_requested:
                try:
                    params = {"chat_id": int(bot.settings["owner_id"]), "text": body, "reply_markup": markup}
                    if panel_id:
                        params["message_id"] = panel_id
                        bot.client.call("editMessageText", **params)
                    else:
                        params["disable_notification"] = True
                        if self.panel_reply_to:
                            params["reply_parameters"] = {"message_id":self.panel_reply_to, "allow_sending_without_reply":True}
                        receipt = bot.client.call("sendMessage", **params)
                        bot.store.change(lambda saved: saved.update(panel_message_id=receipt["message_id"], panel_live=True))
                    self.panel_body = signature
                    self.panel_requested = False
                    self.panel_new = False
                    self.panel_target = None
                    self.panel_reply_to = None
                except TelegramError as error:
                    if error.not_modified:
                        self.panel_body = signature
                        self.panel_requested = False
                        self.panel_target = None
                    elif error.message_missing:
                        self.panel_new = True
                        self.panel_requested = True
                    self.next_panel = now + max(5, error.retry_after)
        if bot.settings.get("notifications", True):
            # Only polling determines connectivity. Optional preview errors are not outages.
            failed = bool(bot.network_errors.get("getUpdates"))
            if failed and self.outage is None:
                self.outage = now
            elif not failed and bot.last_contact and self.outage is not None:
                duration = int(now - self.outage)
                self.outage = None
                if duration >= 10:
                    bot._reply(f"Связь Telegram восстановлена после {duration} с. Очередь сохранена.")
            if not self.started_notice and bot.last_contact:
                self.started_notice = True
                bot._reply("KID запущен и принимает запросы. Панель: /panel. Для работы ботом компьютер и KID должны оставаться включёнными.")

    def run(self):
        bot = self.bot
        next_tick = 0
        last_current = None
        while not bot.stop_event.is_set():
            bot.live_ready.clear()
            try:
                # Token callbacks only change RAM. They never trigger disk reads
                # or synchronous network calls on the Codex reader thread.
                control = None
                if bot.controls_ready.is_set():
                    bot.controls_ready.clear()
                    with bot.lock:
                        state = bot.store.state()
                        controls = [i for i in state["inbox"] if i["kind"] != "prompt"]
                        if controls:
                            control = controls[0]
                            bot.store.change(lambda saved: saved["inbox"].remove(control))
                            if len(controls) > 1:
                                bot.controls_ready.set()
                if control:
                    bot._control(control)
                    bot.work_ready.set()
                    continue
                if bot.current != last_current:
                    last_current = bot.current
                    next_tick = 0
                if time.monotonic() >= next_tick or self.panel_requested:
                    self.tick()
                    next_tick = time.monotonic() + (.5 if bot.current else 5)
            except TelegramError as error:
                next_tick = time.monotonic() + error.retry_after
            except Exception:
                bot.log("[TELEGRAM] Не удалось обновить панель или предпросмотр")
                next_tick = time.monotonic() + 5
            bot.live_ready.wait(max(.01, next_tick - time.monotonic()))
