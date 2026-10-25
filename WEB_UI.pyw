"""
Запуск автономного десктопного окна Windows с интерфейсом v0
(автозапуск и запуск двойным кликом — pythonw, без консоли).

Вся реализация приложения (HTTP-мост :5000, Next.js :3000, WebView-окно,
трей, хоткеи) живёт в desktop_app.py. Раньше WEB_UI.pyw был самостоятельной
копией и разошёлся с desktop_app.py по API: теперь оба лаунчера вызывают
общий main() и делят один BridgeHandler — поведение идентично.
"""

from desktop_app import BridgeHandler, main

if __name__ == "__main__":
    main(window_title="Assistant", console=False)
