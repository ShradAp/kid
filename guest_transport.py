"""Account-free HTTP transport with private proxy settings and bounded work.

No proxy addresses or credentials are interpolated into exceptions or reports.
Importing this module never initializes the application profile or network agent.
"""
import json
import os
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
        routes = [("proxy", self.proxy_url), ("direct", "")] if self.proxy_url else [("direct", "")]
        for route, proxy in routes:
            target = url
            for _ in range(4):
                self._check(target)
                remaining = self.deadline - time.monotonic()
                # Reserve time for a direct attempt if the proxy is broken.
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
                    if response.status_code in (403, 407, 429, 500, 502, 503, 504) and route == "proxy":
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
