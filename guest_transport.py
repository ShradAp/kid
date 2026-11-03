"""Account-free HTTP transport with private proxy settings and bounded work.

No proxy addresses or credentials are interpolated into exceptions or reports.
Importing this module never initializes the application profile or network agent.
"""
import json
import os
import re
from pathlib import Path
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests

from net_guard import is_safe_target


def private_settings():
    root = os.environ.get("KID_CONFIG_DIR")
    if not root:
        if os.name == "nt":
            root = str(Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "kid")
        else:
            root = str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kid")
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def configured_proxy():
    cfg = private_settings()
    mode = cfg.get("proxy_mode", "auto")
    if mode == "direct":
        return ""
    address = str(cfg.get("proxy_url") or os.environ.get("KID_PROXY_URL") or "").strip()
    if not address and mode == "auto":
        from urllib.request import getproxies
        proxies = getproxies()
        address = proxies.get("https") or proxies.get("http") or proxies.get("all") or ""
    if not address:
        return ""
    try:
        parsed = urlsplit(address if "://" in address else "http://" + address)
        if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname:
            return ""
        username = cfg.get("proxy_user") or os.environ.get("KID_PROXY_USER") or unquote(parsed.username or "")
        password = cfg.get("proxy_pass") or os.environ.get("KID_PROXY_PASS") or unquote(parsed.password or "")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        credentials = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@" if username or password else ""
        return urlunsplit((parsed.scheme, credentials + host, "", "", ""))
    except ValueError:
        return ""


class GuestTransportError(Exception):
    pass


def regional_block(response):
    """Recognize explicit regional refusals, without domain-specific rules."""
    if response.status_code == 451:
        return True
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and not any(kind in content_type for kind in ("text/", "json", "xml")):
        return False
    # Some apps embed machine-readable regional errors in hydration JSON,
    # far beyond the page headings. Match generic error codes, never domains.
    if re.search(rb'''["'][a-z0-9_ -]*(?:cross[_ -]border[_ -]block|geo[_ -](?:blocked|restricted))[a-z0-9_ -]*["']''',
                 response.content, re.I):
        return True
    text = response.content[:65536].decode("utf-8", errors="replace")
    # For long HTML pages inspect headings only: an article quoting an error
    # should not change the route of an otherwise successful page.
    if len(text) > 8000 and "html" in content_type:
        text = " ".join(re.findall(r"<(?:title|h[1-3])\b[^>]*>(.*?)</(?:title|h[1-3])>", text, re.I | re.S))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).lower().replace("_", " ")
    return bool(re.search(
        r"(?:not available|unavailable|not supported|not accessible|access (?:is )?(?:denied|restricted|blocked))"
        r".{0,60}(?:your|this) (?:country|region|location)|"
        r"(?:country|region|location).{0,40}(?:not supported|not allowed|blocked)|"
        r"(?:unsupported|restricted|blocked) (?:country|region|location)|"
        r"(?:недоступ[а-я]*|доступ.{0,15}(?:ограничен|запрещён|запрещен|заблокирован))"
        r".{0,60}(?:ваш[а-я]*|данн[а-я]*|эт[а-я]*) (?:стран[а-я]*|регион[а-я]*)|"
        r"(?:geo[ _-]?(?:blocked|restricted)|regional[ _-]restriction)", text))


def browser_challenge(response):
    """A JS/access interstitial is not a successfully read page."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and b"<html" not in response.content[:1024].lower():
        return False
    text = response.content[:100000].decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip().lower()
    if len(text) > 1000:
        return False
    return bool(re.search(r"checking your browser|verify (?:that )?you are human|"
                         r"enable javascript|javascript (?:is )?required|just a moment|"
                         r"access denied|проверка браузера|подтвердите.{0,20}не робот", text)
                or re.fullmatch(r"please wait[.!…\s]*", text))


class GuestSession(requests.Session):
    """A requests-compatible session for adapters and youtube-transcript-api.

    One deadline covers ALL requests, proxy/direct fallback and redirects.
    Bounds count actual outbound attempts, rather than library calls.
    """
    def __init__(self, timeout=18, *, proxy_url=None, max_requests=8, max_bytes=8_000_000,
                 allowed_domains=(), max_response_bytes=4_000_000, anonymous_cookies=()):
        super().__init__()
        self.trust_env = False  # In particular: never load .netrc accounts.
        self.proxy_url = configured_proxy() if proxy_url is None else proxy_url
        self.strict_proxy = os.environ.get("KID_STRICT_PROXY") == "1"
        self.deadline = time.monotonic() + max(0, timeout)
        self.max_requests, self.max_bytes = max_requests, max_bytes
        self.max_response_bytes = max_response_bytes
        self.allowed_domains = allowed_domains
        self.request_count, self.byte_count, self.attempts = 0, 0, []
        self.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "ru,en;q=0.8"})
        # Only for SDK-generated cookies from a fresh guest session, never files.
        self.anonymous_cookies = tuple(anonymous_cookies)

    def _check(self, url):
        if time.monotonic() >= self.deadline:
            raise GuestTransportError("deadline")
        if self.request_count >= self.max_requests:
            raise GuestTransportError("request_limit")
        host = (urlsplit(url).hostname or "").lower()
        if self.allowed_domains and not any(host == d or host.endswith("." + d) for d in self.allowed_domains):
            raise GuestTransportError("unexpected_domain")
        if not is_safe_target(url):
            raise GuestTransportError("unsafe_target")

    def request(self, method, url, **kwargs):
        kwargs.pop("allow_redirects", None)
        kwargs.pop("stream", None)
        kwargs.pop("proxies", None)
        kwargs.pop("timeout", None)
        if kwargs.get("auth") or any(k.lower() in ("authorization", "cookie", "proxy-authorization") for k in kwargs.get("headers", {})):
            raise GuestTransportError("account_credentials_forbidden")
        if self.strict_proxy and not self.proxy_url:
            raise GuestTransportError("proxy_required")
        routes = [("proxy", self.proxy_url)] if self.strict_proxy else [("direct", "")]
        # Only safe reads may be repeated; never replay a form submission.
        if not self.strict_proxy and self.proxy_url and method.upper() in ("GET", "HEAD"):
            routes.append(("proxy", self.proxy_url))
        for route, proxy in routes:
            can_fallback = route == "direct" and len(routes) > 1
            target = url
            for _ in range(4):
                self._check(target)
                remaining = self.deadline - time.monotonic()
                # Keep the shared deadline bounded and leave time for fallback.
                connect_timeout = min(3, max(.05, remaining / 2))
                read_timeout = min(5, max(.05, remaining / 2))
                # Start with no supplied account cookies. Keep only anonymous
                # cookies issued during this short-lived session (e.g. consent).
                if self.request_count == 0:
                    self.cookies.clear()
                    for cookie in self.anonymous_cookies:
                        self.cookies.set_cookie(cookie)
                self.request_count += 1
                response = None
                try:
                    response = super().request(method, target, allow_redirects=False, stream=True,
                        proxies={"http": proxy, "https": proxy} if proxy else {"http": None, "https": None},
                        timeout=(connect_timeout, read_timeout), **kwargs)
                    self.attempts.append({"route": route, "status": response.status_code})
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(target, response.headers.get("Location", ""))
                        response.close()
                        continue
                    if response.status_code in (403, 408, 451, 500, 502, 503, 504) and can_fallback:
                        self.attempts[-1]["reason"] = "http_refusal"
                        response.close()
                        break
                    raw = bytearray()
                    limit = 1_000_000 if "/api/timedtext" in target else self.max_response_bytes
                    for chunk in response.iter_content(65536):
                        if time.monotonic() >= self.deadline:
                            raise GuestTransportError("deadline")
                        self.byte_count += len(chunk)
                        raw.extend(chunk)
                        if len(raw) > limit or self.byte_count > self.max_bytes:
                            raise GuestTransportError("byte_limit")
                    response.close()
                    response._content = bytes(raw)
                    response._content_consumed = True
                    response.encoding = "utf-8"
                    refusal = "regional_block" if regional_block(response) else "browser_challenge" if browser_challenge(response) else ""
                    if refusal:
                        self.attempts[-1]["reason"] = refusal
                        if can_fallback:
                            break
                        raise GuestTransportError(refusal)
                    response.kid_route = route
                    return response
                except GuestTransportError:
                    if response is not None:
                        response.close()
                    raise
                except requests.RequestException as exc:
                    if response is not None:
                        response.close()
                    self.attempts.append({"route": route, "error": type(exc).__name__})
                    break
            else:
                raise GuestTransportError("redirect_limit")
        raise GuestTransportError("network_unavailable")

    def diagnostics(self):
        return {"requests": self.request_count, "bytes": self.byte_count, "attempts": list(self.attempts)}
