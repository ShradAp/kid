"""
Определение рабочего прокси для сетевого агента.

Раньше адрес прокси был захардкожен (http://127.0.0.1:10808): если у
пользователя прокси в настройках Windows слушает на другом порту (или
вовсе не запущен), Chromium падал с net::ERR_PROXY_CONNECTION_FAILED
и инструменты fetch_webpage/web_search не работали.

Порядок выбора:
1. явно заданный адрес (аргумент или env KID_PROXY_URL),
2. системный прокси Windows (реестр WinINET — как это делает urllib),
3. прямой доступ без прокси.

Каждый кандидат проверяется быстрым TCP-подключением (socket), чтобы
не отправлять браузер к мёртвому адресу.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

logger = logging.getLogger("NetworkAgent.ProxyEnv")

# Признаки того, что ошибка загрузки страницы связана с прокси
PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_",
    "ERR_SOCKS_CONNECTION",
    "ERR_MANDATORY_PROXY_CONFIGURATION",
)

_alive_ok: dict[str, float] = {}
_alive_fail: dict[str, float] = {}
_alive_lock = threading.Lock()
_ALIVE_TTL = 600.0  # живой адрес не перепроверяем 10 минут
_DEAD_TTL = 30.0    # мёртвый не перепроверяем чаще раза в 30 секунд


def _normalize_proxy_url(raw) -> str:
    """Приводит адрес прокси к виду scheme://host:port (форматы WinINET)."""
    if not raw:
        return ""
    s = str(raw).strip().rstrip(";, ")
    if not s:
        return ""
    # Формат Windows ProxyServer: "http=1.2.3.4:8080;https=1.2.3.4:8080"
    if "=" in s and "://" not in s.split(";")[0]:
        for part in s.split(";"):
            key, _, value = part.partition("=")
            if key.strip().lower() in ("http", "https") and value.strip():
                s = value.strip()
                break
    if "://" not in s:
        s = "http://" + s
    return s


def detect_system_proxy() -> str:
    """Прокси из настроек Windows (реестр HKCU\\...\\Internet Settings)."""
    try:
        import urllib.request

        proxies = urllib.request.getproxies()
    except Exception:
        return ""
    for key in ("https", "http", "all"):
        raw = proxies.get(key)
        if raw:
            return _normalize_proxy_url(raw)
    return ""


def _tcp_probe(proxy_url: str, timeout: float) -> bool:
    """TCP-проверка: порт прокси вообще слушается."""
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        port = parsed.port or (1080 if parsed.scheme.startswith("socks") else 80)
        if not host:
            return False
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return True
    except Exception:
        return False


def _http_verify(proxy_url: str, timeout: float = 7.0) -> tuple[bool, str]:
    """Реальная проверка: GET https://example.com ЧЕРЕЗ кандидат-прокси.

    TCP-порт может слушаться (например, прокси с авторизацией в настройках
    Windows), но трафик через него всё равно не идёт — именно это давало
    407 Proxy Authentication Required и net::ERR_PROXY_CONNECTION_FAILED.
    """
    try:
        import requests

        session = requests.Session()
        session.trust_env = False
        resp = session.get(
            "https://example.com",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        if resp.status_code in (200, 301, 302):
            return True, ""
        if resp.status_code == 407:
            return False, "требует авторизацию (407)"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


def proxy_alive(proxy_url: str, timeout: float = 1.5) -> bool:
    """Прокси пригоден: TCP-порт слушается И через него реально ходит трафик."""
    if not proxy_url:
        return False
    now = time.time()
    with _alive_lock:
        ok_stamp = _alive_ok.get(proxy_url)
        if ok_stamp and now - ok_stamp < _ALIVE_TTL:
            return True
        fail_stamp = _alive_fail.get(proxy_url)
        if fail_stamp and now - fail_stamp < _DEAD_TTL:
            return False
    ok = _tcp_probe(proxy_url, timeout) and _http_verify(proxy_url)[0]
    with _alive_lock:
        if ok:
            _alive_ok[proxy_url] = now
        else:
            _alive_fail[proxy_url] = now
    return ok


def parse_proxy_auth(proxy_url: str) -> tuple[str, str, str]:
    """Отделяет логин/пароль: scheme://user:pass@host:port → (server, user, pass)."""
    try:
        parsed = urlparse(proxy_url)
        username = parsed.username or ""
        password = parsed.password or ""
        if not username and not password:
            return proxy_url, "", ""
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return f"{parsed.scheme or 'http'}://{netloc}", username, password
    except Exception:
        return proxy_url, "", ""


def assemble_proxy(address: str, username: str = "", password: str = "") -> str:
    """Собирает полный адрес прокси из полей вкладки «Прокси».

    address — «127.0.0.1:8080» или «http://host:port» (может уже содержать
    user:pass@), username/password — отдельные поля логина/пароля.
    """
    normalized = _normalize_proxy_url(address)
    if not normalized:
        return ""
    if not username and not password:
        return normalized
    try:
        parsed = urlparse(normalized)
        host = parsed.hostname or ""
        if not host:
            return normalized
        scheme = parsed.scheme or "http"
        userinfo = quote(str(username), safe="") if username else ""
        if password:
            userinfo = (userinfo + ":" + quote(str(password), safe="")) if userinfo else ":" + quote(str(password), safe="")
        netloc = f"{userinfo}@{host}" if userinfo else host
        if parsed.port:
            netloc += f":{parsed.port}"
        return f"{scheme}://{netloc}"
    except Exception:
        return normalized


def is_proxy_error(text: str) -> bool:
    """Похожа ли ошибка на сбой прокси (по маркерам Chromium/Playwright)."""
    t = str(text or "").lower()
    if not t:
        return False
    return any(marker.lower() in t for marker in PROXY_ERROR_MARKERS)


def resolve_working_proxy(configured: str = "", log_cb=None) -> str:
    """Возвращает РАБОЧИЙ прокси или '' (прямой доступ).

    Порядок: заданный → env KID_PROXY_URL → системный Windows → прямой доступ.
    Проверка реальным HTTP-запросом через прокси: порт может слушаться,
    но трафик не идти (407/таймаут) — такой кандидат пропускается.
    """
    def _log(message: str) -> None:
        logger.info(message)
        if log_cb:
            try:
                log_cb(message)
            except Exception:
                pass

    candidates = [
        _normalize_proxy_url(configured),
        _normalize_proxy_url(os.environ.get("KID_PROXY_URL", "")),
        detect_system_proxy(),
    ]
    tried: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        if _tcp_probe(candidate, 1.5):
            ok, why = _http_verify(candidate)
            if ok:
                _log(f"🌐 Прокси: используется {candidate} (проверен запросом)")
                return candidate
            _log(f"⚠️ Прокси: {candidate} непригоден ({why})")
        else:
            _log(f"⚠️ Прокси: {candidate} не отвечает (порт закрыт)")
    if tried:
        _log("🌐 Прокси: рабочие адреса не найдены — прямой доступ")
    else:
        _log("🌐 Прокси: не настроен — прямой доступ")
    return ""


# ==================== РАСШИРЕННЫЕ НАСТРОЙКИ ПРОКСИ (авторизация + гео-профиль) ====================

DEFAULT_PROXY_TIMEZONE = "Europe/Tallinn"
DEFAULT_PROXY_LOCALE = "et-EE"
DEFAULT_PROXY_ACCEPT_LANGUAGE = "et-EE,et;q=0.9,ru-RU;q=0.8,en-US;q=0.7"


@dataclass
class ProxySettings:
    """Полные настройки прокси: адрес, авторизация и гео-профиль браузера."""

    server: str = ""          # scheme://host:port БЕЗ учётных данных
    username: str = ""
    password: str = ""
    timezone: str = DEFAULT_PROXY_TIMEZONE
    locale: str = DEFAULT_PROXY_LOCALE
    accept_language: str = DEFAULT_PROXY_ACCEPT_LANGUAGE

    @property
    def full_url(self) -> str:
        """Полный адрес прокси со встроенной авторизацией (для requests/curl_cffi)."""
        return assemble_proxy(self.server, self.username, self.password)

    @property
    def enabled(self) -> bool:
        return bool(self.server)


def load_proxy_settings(config: dict | None = None) -> ProxySettings:
    """Собирает настройки прокси из конфигурации и переменных окружения.

    Адрес можно задать полным URL http://user:password@host:port либо отдельными
    полями (proxy_url / proxy_user / proxy_pass). Таймзона, локаль и Accept-Language
    читаются из отдельных полей конфигурации или env-переменных; при отсутствии
    используются дефолты (Europe/Tallinn, et-EE).
    """
    cfg = config if isinstance(config, dict) else {}

    def _pick(cfg_key: str, env_key: str, default: str = "") -> str:
        value = str(cfg.get(cfg_key) or "").strip()
        if value:
            return value
        return os.environ.get(env_key, "").strip() or default

    address = _pick("proxy_url", "KID_PROXY_URL")
    username = _pick("proxy_user", "KID_PROXY_USER")
    password = _pick("proxy_pass", "KID_PROXY_PASS")
    timezone = _pick("proxy_timezone", "KID_PROXY_TIMEZONE", DEFAULT_PROXY_TIMEZONE)
    locale = _pick("proxy_locale", "KID_PROXY_LOCALE", DEFAULT_PROXY_LOCALE)
    accept_language = _pick(
        "proxy_accept_language", "KID_PROXY_ACCEPT_LANGUAGE", DEFAULT_PROXY_ACCEPT_LANGUAGE
    )

    # Полный URL http://user:pass@host:port — извлекаем учётки, если их не задали
    # отдельными полями.
    normalized = _normalize_proxy_url(address)
    server, embedded_user, embedded_pass = (
        parse_proxy_auth(normalized) if normalized else ("", "", "")
    )
    username = username or embedded_user
    password = password or embedded_pass

    return ProxySettings(
        server=server,
        username=username,
        password=password,
        timezone=timezone,
        locale=locale,
        accept_language=accept_language,
    )
