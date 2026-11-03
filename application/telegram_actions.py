"""Authenticated local HTTP settings for the optional Telegram module."""

import json


def handle(request, bot, method):
    try:
        if method == "GET":
            result = bot.public_settings()
        else:
            data = json.loads(request.rfile.read(int(request.headers.get("Content-Length", 0))).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект настроек Telegram")
            if request.path == "/api/telegram/clear":
                bot.clear()
                result = bot.public_settings()
            elif request.path == "/api/telegram/stop":
                bot.stop_turn()
                result = bot.public_settings()
            elif request.path == "/api/telegram/test-file":
                result = bot.test_file(data.get("direction", "send"))
            elif request.path == "/api/telegram/test-integration":
                result = bot.test_integration(data.get("kind"))
            elif request.path == "/api/telegram/panel":
                if not bot.settings or bot.stop_event.is_set() or not bot.settings["enabled"]:
                    raise ValueError("Сначала включите Telegram")
                bot.live.request_panel()
                result = bot.public_settings()
            elif request.path == "/api/telegram/archive/clear":
                if data.get("confirm") is not True:
                    raise ValueError("Подтвердите удаление файлов архива")
                removed = bot.clear_archive()
                result = {**bot.public_settings(), "archive_removed": removed}
            else:
                result = bot.configure(data)
        request._set_cors()
        request.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except ValueError as error:
        request._set_cors(400)
        # JSON decoding errors can quote request content; never return it.
        message = "Некорректный JSON" if isinstance(error, json.JSONDecodeError) else str(error)
        request.wfile.write(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    except Exception:
        request._set_cors(503)
        request.wfile.write(json.dumps({"error": "Модуль Telegram недоступен; проверьте локальное хранилище"}, ensure_ascii=False).encode("utf-8"))
