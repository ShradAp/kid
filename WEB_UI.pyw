"""
Запуск автономного десктопного окна Windows с интерфейсом v0
(автозапуск и запуск двойным кликом — pythonw, без консоли).

Вся реализация приложения (HTTP-мост :5000, Next.js :3000, WebView-окно,
трей, хоткеи) живёт в desktop_app.py. Раньше WEB_UI.pyw был самостоятельной
копией и разошёлся с desktop_app.py по API: теперь оба лаунчера вызывают
общий main() и делят один BridgeHandler — поведение идентично.
"""

import os
import subprocess
import sys
from pathlib import Path


def launch():
    # Ассоциация .pyw может указывать на другой Python без пакетов проекта.
    root = Path(__file__).resolve().parent
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if pythonw.is_file() and Path(sys.prefix).resolve() != (root / ".venv").resolve():
        subprocess.Popen([str(pythonw), str(root / "WEB_UI.pyw")], cwd=str(root),
                         creationflags=0x08000000 if os.name == "nt" else 0)
        return
    try:
        from desktop_app import main

        main(window_title="Assistant", console=False)
    except BaseException as exc:
        if isinstance(exc, SystemExit) and not exc.code:
            return
        from app_paths import USER_DATA_DIR
        from datetime import datetime
        import traceback

        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = USER_DATA_DIR / "last_error.log"
        if not isinstance(exc, SystemExit):
            path.write_text(f"[{datetime.now():%d.%m.%Y %H:%M:%S}] {traceback.format_exc()}", encoding="utf-8")
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, f"Не удалось запустить kid. Подробности:\n{path}", "kid — ошибка запуска", 0x10)

if __name__ == "__main__":
    launch()
