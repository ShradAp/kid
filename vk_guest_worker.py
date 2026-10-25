"""Fresh browser context for one public VK post; no user profile or author crawl."""
import json
import sys
import time
from urllib.parse import unquote, urlsplit


def run(payload):
    from playwright.sync_api import sync_playwright
    from net_guard import is_safe_target
    from vk_public import exact_post
    deadline = payload.get("deadline", time.monotonic() + payload["timeout"])-.5
    target_id = f"{payload['owner']}_{payload['post']}"
    found, checked = [], {}
    request_count = 0
    def guard(route):
        nonlocal request_count
        request_count += 1
        host = urlsplit(route.request.url).hostname or ""
        if host not in checked:
            checked[host] = is_safe_target(route.request.url)
        if (time.monotonic() >= deadline or request_count > 40 or not checked[host]
                or route.request.resource_type in ("image", "media", "font", "websocket")):
            route.abort()
        else:
            route.continue_()
    def inspect(response):
        if found or "json" not in response.headers.get("content-type", ""):
            return
        host = urlsplit(response.url).hostname or ""
        if not any(host == d or host.endswith("."+d) for d in ("vk.com", "vk.ru")):
            return
        try:
            body = response.body()
            if len(body) <= 1_000_000:
                text = exact_post(json.loads(body), payload["owner"], payload["post"])
                if text:
                    found.append(text)
        except Exception:
            pass
    launch = {"headless": True}
    if payload.get("proxy"):
        parsed = urlsplit(payload["proxy"])
        launch["proxy"] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                           "username": unquote(parsed.username or ""), "password": unquote(parsed.password or "")}
    if not is_safe_target(payload["url"]):
        return {"status": "unsafe_target"}
    with sync_playwright() as api:
        browser = api.chromium.launch(**launch)
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False, locale="ru-RU")
            context.route("**/*", guard)
            if hasattr(context, "route_web_socket"):
                context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            page.on("popup", lambda popup: popup.close())
            page.on("response", inspect)
            try:
                page.goto(payload["url"], wait_until="domcontentloaded", timeout=max(100, min(8000, int((deadline-time.monotonic())*1000))))
            except Exception:
                pass
            selector = f'[id="post{target_id}"] .wall_post_text, [id="post{target_id}"] .pi_text, [data-post-id="{target_id}"] [data-testid="post_text"]'
            while time.monotonic() < deadline:
                if found:
                    return {"status": "read_network_response", "text": found[0]}
                nodes = page.locator(selector)
                if nodes.count():
                    text = nodes.first.inner_text(timeout=500).strip()
                    if text:
                        return {"status": "read_dom", "text": text[:12000]}
                page.wait_for_timeout(200)
            return {"status": "post_not_exposed"}
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        result = run(json.loads(sys.stdin.read()))
    except Exception as exc:
        result = {"status": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True))
