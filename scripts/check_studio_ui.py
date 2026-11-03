"""Offline Studio workflow with real temporary DOCX, ephemeral ports and Chromium.

Run from a disposable source/build copy; no main(), real account or external API.
Browser requests outside this frontend are blocked and API calls go to its own
authenticated Python bridge. Native file dialogs and Windows actions are unused.
"""
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from docx import Document
    from playwright.sync_api import sync_playwright, expect

    with tempfile.TemporaryDirectory(prefix="kid-studio-ui-") as directory:
        os.environ["KID_CONFIG_DIR"] = directory
        os.environ["KID_WORD_PRESETS_DIR"] = str(Path(directory) / "presets")
        Path(directory, "config.json").write_text(json.dumps({
            "active_profile": "main", "profiles": {"main": {"engine": "api", "api_key": "", "model": "fixture"}},
        }), encoding="utf-8")
        import desktop_app as app

        app.get_live_balance = lambda *args, **kwargs: None
        app.user_profile_memory = None
        source = Path(directory) / "studio-fixture.docx"
        document = Document()
        document.add_heading("Studio fixture", level=1)
        document.add_paragraph("Preserve this text while changing formatting.")
        document.save(source)
        app.FILE_ACCESS.grant(str(source))
        analysis = app._word_analyze_payload({"source_file": str(source), "session_id": "document-studio"})
        preset_name = analysis["preset"]["name"]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.BridgeHandler)
        app.BRIDGE_PORT = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            frontend_port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{frontend_port}"
        environment = dict(os.environ, KID_BRIDGE_TOKEN=app.BRIDGE_TOKEN,
                           KID_BRIDGE_PORT=str(server.server_port), NEXT_TELEMETRY_DISABLED="1")
        with tempfile.TemporaryFile() as log:
            frontend = subprocess.Popen(
                ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", str(frontend_port)],
                cwd=ROOT / "UI", env=environment, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    if frontend.poll() is not None:
                        raise RuntimeError("Studio frontend exited during startup")
                    try:
                        local.open(origin, timeout=1).close()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Studio frontend did not start")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 1600, "height": 1100})

                        def route_local(route):
                            if not route.request.url.startswith(origin + "/"):
                                route.abort()
                                return
                            url = urlsplit(route.request.url)
                            if not url.path.startswith("/api/"):
                                route.continue_()
                                return
                            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                            try:
                                headers = {"X-Kid-Token": app.BRIDGE_TOKEN}
                                if route.request.headers.get("content-type"):
                                    headers["Content-Type"] = route.request.headers["content-type"]
                                connection.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                                   body=route.request.post_data_buffer, headers=headers)
                                response = connection.getresponse()
                                route.fulfill(status=response.status,
                                              content_type=response.getheader("Content-Type", "application/json"),
                                              body=response.read())
                            finally:
                                connection.close()

                        context.route("**/*", route_local)
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin)
                        page.get_by_role("button", name="Открыть Document Studio", exact=True).click()
                        margin = page.locator('[data-slot="field"]').filter(
                            has=page.get_by_text("Левое", exact=True)
                        ).get_by_role("spinbutton")
                        expect(margin).to_be_visible(timeout=15000)
                        margin.fill("28")
                        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/word/apply") as applied:
                            page.get_by_role("button", name="Применить пресет и правила", exact=True).click()
                        assert applied.value.status == 200
                        expect(page.get_by_role("button", name="Применить пресет и правила", exact=True)).to_be_enabled()
                        applied_path = Path(applied.value.json()["path"])
                        assert applied_path.is_relative_to(Path(directory))
                        updated = Document(applied_path)
                        assert abs(updated.sections[0].left_margin.mm - 28) < .1
                        assert any("Preserve this text" in p.text for p in updated.paragraphs)
                        page.get_by_placeholder(preset_name, exact=True).fill("ui-fixture")
                        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/word/presets"
                                                  and response.request.method == "POST") as saved:
                            page.get_by_role("button", name="Сохранить пресет", exact=True).click()
                        assert saved.value.status == 200
                        assert Path(directory, "presets", "ui-fixture.json").is_file()
                        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/word/close") as closed:
                            page.get_by_role("button", name="Закрыть документ", exact=True).click()
                        assert closed.value.status == 200
                        assert source.is_file()
                        assert not app._word_state_payload({"session_id": "document-studio"})["has_document"]
                        assert not errors, errors
                        page.goto("about:blank")
                        context.unroute_all(behavior="wait")
                    finally:
                        browser.close()
                print("Studio UI smoke passed: restore document, edit margin, apply real DOCX, save preset, close without deleting source")
            finally:
                frontend.terminate()
                try:
                    frontend.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    frontend.kill()
                    frontend.wait(timeout=5)
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                if sys.exc_info()[0] is not None:
                    log.seek(0)
                    print(log.read(8000).decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
