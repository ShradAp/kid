"""Short-lived, account-free JavaScript reader with explicit proxy routing.

No persistent browser profile, domain exceptions, credentials in diagnostics,
or direct fallback after a configured proxy fails.
"""
import asyncio
import html
import re
import time
from types import SimpleNamespace
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from guest_transport import configured_proxy, regional_block, browser_challenge
from net_guard import is_safe_target


def non_text_resource(request):
    """Media can arrive as fetch/XHR, not only as Chromium's media type."""
    return (request.resource_type in ("image", "font", "media")
            or "range" in request.headers
            or bool(re.search(r"\.(?:mp4|webm|m4s|m3u8|mpd|mp3|m4a|wav|woff2?|ttf|png|jpe?g|gif|webp|avif)$",
                              urlsplit(request.url).path, re.I)))


def unreadable_page(text):
    """Classify short refusal screens, not login buttons in real articles."""
    text = re.sub(r"\s+", " ", text).strip().lower()
    if re.fullmatch(r"(?:log ?in|sign ?in|войти|вход)[.!\s]*", text):
        return "login_screen"
    if len(text) < 500 and re.search(r"(?:unable|failed) to play|playback error|video (?:is )?unavailable|"
                                      r"ошибка воспроизведения|не удалось воспроизвести", text):
        return "playback_unavailable"
    return ""


def proxy_options(address):
    if not address:
        return None
    parsed = urlsplit(address)
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    if scheme not in ("http", "https", "socks5") or not parsed.hostname:
        raise ValueError("invalid_proxy")
    if scheme == "socks5" and (parsed.username or parsed.password):
        raise ValueError("browser_proxy_auth_unsupported")
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    if parsed.port:
        authority += f":{parsed.port}"
    result = {"server": urlunsplit((scheme, authority, "", "", "")), "bypass": "<-loopback>"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def read_page(url, cancel, *, proxy_url=None, timeout=25):
    address = configured_proxy() if proxy_url is None else proxy_url
    route = "proxy" if address else "direct"
    if cancel.is_set():
        return {"status": "unavailable", "reason": "cancelled", "route": route}
    try:
        proxy = proxy_options(address)
    except (ValueError, TypeError):
        return {"status": "unavailable", "reason": "browser_proxy_unsupported", "route": route}
    try:
        return asyncio.run(_read(url, cancel, proxy, timeout))
    except Exception:
        return {"status": "unavailable", "reason": "browser_unavailable", "route": route, "read_method": "browser"}


async def _read(url, cancel, proxy, timeout):
    from playwright.async_api import async_playwright
    route_name = "proxy" if proxy else "direct"
    budget = {"requests": 0, "bytes": 0, "skipped_resources": 0, "by_type": {}, "javascript_errors": 0}
    deadline = time.monotonic() + max(.1, min(timeout, 40))
    failure = ""
    resource_limit = False

    def result(status="unavailable", reason="", **extra):
        return {"status": status, "reason": reason, "read_method": "browser",
                "visual_opened": False, "route": route_name, "browser_transport": dict(budget),
                "resource_limit_reached": resource_limit, **extra}

    async def run():
        nonlocal failure
        if not await asyncio.to_thread(is_safe_target, url):
            return result(reason="unsafe_target")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, proxy=proxy,
                args=["--disable-quic", "--disable-background-networking"] + ([] if proxy else ["--proxy-server=direct://"]))
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                page = await context.new_page()
                def on_page_error(_):
                    budget["javascript_errors"] += 1
                page.on("pageerror", on_page_error)

                async def intercept(route):
                    nonlocal failure, resource_limit
                    request = route.request
                    if failure or resource_limit:
                        await route.abort()
                        return
                    if cancel.is_set() or time.monotonic() >= deadline:
                        failure = "cancelled" if cancel.is_set() else "deadline"
                        await route.abort()
                        return
                    if non_text_resource(request):
                        budget["skipped_resources"] += 1
                        await route.abort()
                        return
                    if not await asyncio.to_thread(is_safe_target, request.url):
                        failure = "unsafe_target"
                        await route.abort()
                        return
                    if budget["requests"] >= 160 or budget["bytes"] >= 16_000_000:
                        resource_limit = True
                        await route.abort()
                        return
                    budget["requests"] += 1
                    kind = budget["by_type"].setdefault(request.resource_type, {"requests": 0, "bytes": 0})
                    kind["requests"] += 1
                    response = None
                    try:
                        # Chromium must never follow unchecked HTTP redirects.
                        target = request.url
                        for _ in range(4):
                            response = await route.fetch(url=target, max_redirects=0, max_retries=0,
                                timeout=max(1, int((deadline - time.monotonic()) * 1000)))
                            if response.status not in (301, 302, 303, 307, 308):
                                break
                            target = urljoin(target, response.headers.get("location", ""))
                            if not await asyncio.to_thread(is_safe_target, target):
                                failure = "unsafe_target"
                                await route.abort()
                                return
                            elif request.is_navigation_request() and request.frame == page.main_frame:
                                # A fresh navigation is intercepted again, unlike
                                # Chromium's automatic HTTP redirect chain.
                                await route.fulfill(status=200, content_type="text/html", body=
                                    '<meta http-equiv="refresh" content="0;url=' + html.escape(target, quote=True) + '">')
                                return
                            if budget["requests"] >= 160:
                                resource_limit = True
                                await route.abort()
                                return
                            budget["requests"] += 1
                            await response.dispose()
                            response = None
                        else:
                            failure = "redirect_limit"
                            await route.abort()
                            return
                        if request.is_navigation_request() and request.frame == page.main_frame and response.status >= 400:
                            failure = f"HTTP {response.status}"
                            await route.abort()
                            return
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type.startswith(("video/", "audio/", "image/", "font/")) or any(
                                media in content_type for media in ("application/vnd.apple.mpegurl", "application/dash+xml")):
                            budget["skipped_resources"] += 1
                            await route.abort()
                            return
                        body = await response.body()
                        budget["bytes"] += len(body)
                        kind["bytes"] += len(body)
                        if budget["bytes"] > 16_000_000:
                            resource_limit = True
                            await route.abort()
                            return
                        await route.fulfill(response=response, body=body)
                    except Exception:
                        if request.is_navigation_request():
                            failure = "browser_network_unavailable"
                        await route.abort()
                    finally:
                        if response is not None:
                            await response.dispose()

                await context.route("**/*", intercept)
                await context.route_web_socket("**/*", lambda socket: socket.close())
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=max(1, int(timeout * 1000)))
                except Exception:
                    if failure:
                        return result(reason=failure)
                try:
                    await page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
                text = ""
                refusal = "empty"
                while time.monotonic() < deadline:
                    if failure or cancel.is_set():
                        return result(reason=failure or "cancelled")
                    try:
                        markup = (await page.content()).encode("utf-8")
                        stub = SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, content=markup)
                        text = await page.locator("body").inner_text(timeout=1000)
                        refusal = "regional_block" if regional_block(stub) else "browser_challenge" if browser_challenge(stub) else ""
                        refusal = refusal or unreadable_page(text)
                        if refusal in ("login_screen", "playback_unavailable"):
                            return result(reason=refusal)
                        if text.strip() and not refusal:
                            return result(status="read", text_content=text[:100000], url=page.url, content_kind="page_text",
                                source_truncated=len(text) > 100000, title=await page.title())
                    except Exception:
                        pass  # A JS navigation can replace the document mid-read.
                    await asyncio.sleep(.5)
                return result(reason="browser_budget" if resource_limit else refusal or "empty")
            finally:
                await asyncio.wait_for(browser.close(), timeout=5)

    task = asyncio.create_task(run())
    try:
        while not task.done():
            if cancel.is_set() or time.monotonic() >= deadline:
                task.cancel()
                return result(reason="cancelled" if cancel.is_set() else "browser_budget" if resource_limit else "deadline")
            await asyncio.sleep(.1)
        return await task
    except Exception:
        # Playwright exceptions may include proxy credentials and request data.
        return result(reason="browser_unavailable")
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass
