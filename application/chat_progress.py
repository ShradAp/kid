"""Per-session live output and cancellation shared by both chat engines."""

import json
import threading
import urllib.parse


class ChatProgress:
    def __init__(self):
        self.thinking = {}
        self.reasoning = {}
        self.reply = {}
        self.cancel_events = {}
        self.lock = threading.RLock()

    def write_snapshot(self, request):
        query = urllib.parse.urlparse(request.path).query
        progress_sid = (urllib.parse.parse_qs(query).get("session_id") or [""])[0]
        progress_steps = self.thinking.get(progress_sid)
        progress_reasoning = self.reasoning.get(progress_sid, "")
        request._set_cors()
        request.wfile.write(
            json.dumps(
                {
                    "active": progress_steps is not None,
                    "steps": list(progress_steps) if progress_steps else [],
                    "reasoning": progress_reasoning or "",
                    "reply": self.reply.get(progress_sid, ""),
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )


    def stop(self, request, log_event):
        content_length = int(request.headers.get("Content-Length", 0))
        body = request.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            req_data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            req_data = {}
        stop_sid = str((req_data or {}).get("session_id") or "")
        stop_event = self.cancel_events.get(stop_sid)
        if stop_event is not None:
            stop_event.set()
            log_event(f"[STOP] Запрошена остановка генерации (сессия '{stop_sid}')")
        request._set_cors()
        request.wfile.write(
            json.dumps(
                {"ok": True, "stopped": stop_event is not None},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        return


