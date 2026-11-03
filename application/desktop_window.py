"""WebView window, tray, activation and hotkey registration."""

import os
import threading

import keyboard
import pystray
import webview
from PIL import Image, ImageDraw


class DesktopWindow:
    def __init__(self, shutting_down, shutdown):
        self.shutting_down = shutting_down
        self.shutdown = shutdown
        self.ready = threading.Event()
        self.window = None

    def activate(self):
        # Событие может прийти во время загрузки Next.js или создания WebView.
        self.ready.wait()
        if not self.shutting_down.is_set():
            self.window.show()
            self.window.restore()


    def run(self, window_title, url):
        window = webview.create_window(
            title=window_title,
            url=url,
            width=1240,
            height=860,
            resizable=True,
            text_select=True,  # РАЗРЕШАЕТ ВЫДЕЛЕНИЕ И КОПИРОВАНИЕ ТЕКСТА
            background_color="#0a0a0c",
        )
        self.window = window

        is_visible = True
        tray_icon = None

        def on_shown():
            nonlocal is_visible
            is_visible = True
            self.ready.set()

        window.events.shown += on_shown

        def terminate_all(*_args):
            nonlocal tray_icon
            if not self.shutdown():
                return
            try:
                if tray_icon:
                    tray_icon.stop()
            finally:
                # ОС закрывает Job handle и дочищает всё дерево, включая WebView2.
                os._exit(0)

        def toggle_window():
            nonlocal is_visible
            if is_visible:
                window.hide()
                is_visible = False
            else:
                window.show()
                window.restore()
                is_visible = True

        def create_tray_image():
            img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((8, 8, 56, 56), fill="#6366f1")
            draw.ellipse((22, 22, 42, 42), fill="#ffffff")
            return img

        def setup_tray():
            nonlocal tray_icon
            menu = pystray.Menu(
                pystray.MenuItem("Показать / Скрыть", toggle_window, default=True),
                pystray.MenuItem("Выход", terminate_all),
            )
            tray_icon = pystray.Icon(
                "sprut_assistant", create_tray_image(), "Sprut AI Assistant", menu
            )
            tray_icon.run()

        # Запуск трея в фоновом потоке
        threading.Thread(target=setup_tray, daemon=True).start()

        # Регистрация хоткея
        keyboard.add_hotkey("ctrl+shift+space", toggle_window)

        # Сворачивание прямо в трей
        def on_minimized():
            nonlocal is_visible
            window.hide()
            is_visible = False

        window.events.minimized += on_minimized
        window.events.closed += terminate_all

        webview.start()
        terminate_all()
