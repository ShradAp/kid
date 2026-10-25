"""
Ядро сетевого агента.
Объединяет SearXNG-поиск, Jina Reader и локальный Playwright-браузер с авто-роутингом.
"""

from __future__ import annotations

import base64
import html as html_mod
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests  # TLS-слой с impersonate (обход антибот)

from . import proxy_env
from .browser import BrowserManager
from .tunnel import TunnelClient
from net_guard import (
    MAX_HTML_BYTES,
    MAX_IMAGE_BYTES,
    is_safe_target,
    read_bounded,
)

logger = logging.getLogger("NetworkAgent.Core")

# curl_cffi не имеет ровно "chrome133" (в 0.16.x есть "chrome133a" — Chrome 133,
# альтернативный фингерпринт). Берём ближайший доступный Chrome-таргет, с фолбэком.
def _resolve_impersonate_target() -> str:
    try:
        valid = {e.value for e in requests.BrowserType}
    except Exception:
        valid = set()
    for target in ("chrome133", "chrome133a", "chrome131", "chrome"):
        if not valid or target in valid:
            return target
    return "chrome"


IMPERSONATE_TARGET = _resolve_impersonate_target()

# TTL-кэш прочитанных страниц
_CACHE_TTL_PAGES = 600  # секунд на страницу (10 мин)
_CACHE_MAX_ENTRIES = 200

# Единственный критерий успеха быстрого пути — минимальная длина очищенного
# ТЕЛА страницы. Заголовок и Open Graph на решение больше не влияют.
MIN_BODY_CHARS = 500

# Trafilatura — основной движок извлечения контента (сама распознаёт кодировку и
# отделяет статью от навигационного мусора). Необязательна: при её отсутствии или
# ошибке работает аварийный regex-фолбэк (_html_to_text / _meta_content).
try:
    import trafilatura
except Exception:  # noqa: BLE001 — опциональная зависимость
    trafilatura = None


def _html_to_text(html: str) -> str:
    """Грубое извлечение текста из HTML для быстрого пути (без браузера)."""
    html = re.sub(r"(?is)<(script|style|noscript|svg|head|form)[^>]*>.*?</\1\s*>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>|</h[1-6]\s*>|</tr\s*>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _meta_content(html: str, name: str) -> str:
    """Значение meta[property|name="..."] — og:image, og:description и т.п."""
    m = re.search(
        r'(?is)<meta[^>]+(?:property|name)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']+)',
        html,
    )
    return html_mod.unescape(m.group(1)).strip() if m else ""


def _extract_content(raw, url, encoding="utf-8"):
    """Извлекает (text, title, description, image) из сырых байтов страницы.

    Основной движок — Trafilatura (получает сырые байты и сама распознаёт
    кодировку). При её отсутствии/ошибке — аварийный regex-фолбэк.
    """
    if trafilatura is not None:
        try:
            result = trafilatura.bare_extraction(raw, url=url)
            if result is not None and not isinstance(result, dict):
                result = {key: getattr(result, key, None) for key in ("text", "title", "description", "image")}
            if isinstance(result, dict):
                text = (result.get("text") or "").strip()
                title = (result.get("title") or "").strip()
                description = (result.get("description") or "").strip()
                image = (result.get("image") or "").strip()
                return text, title, description, image
        except Exception as exc:
            logger.info(f"Trafilatura не извлекла {url}: {exc} — fallback на regex")

    html = (raw or b"").decode(encoding or "utf-8", errors="replace")
    text = _html_to_text(html)
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = html_mod.unescape(m.group(1)).strip()[:300]
    description = _meta_content(html, "og:description")
    image = _meta_content(html, "og:image")
    return text, title, description, image


class NetworkAgentCore:
    def __init__(
        self,
        searxng_url: str = "http://127.0.0.1:8080",
        proxy_url: str = "",
        use_vpn: bool = True,
        log_cb=None,
    ):
        self.log_cb = log_cb
        # Полные настройки прокси: адрес + авторизация + гео-профиль (таймзона,
        # локаль, Accept-Language). Читаются из config.json и переменных окружения.
        self.proxy_settings = proxy_env.ProxySettings()
        self.proxy_url = ""
        if use_vpn:
            # Адрес прокси не захардкожен: при явном аргументе — проверяем его,
            # иначе читаем настройки вкладки «Прокси» (config.json), а в режиме
            # «Авто» — env KID_PROXY_URL и системный прокси Windows. Мёртвый
            # адрес давал net::ERR_PROXY_CONNECTION_FAILED на каждой зарубежной
            # странице.
            if proxy_url:
                self.proxy_url = proxy_env.resolve_working_proxy(proxy_url, log_cb)
                self._sync_settings_from_url()
            else:
                self.proxy_url = self._proxy_from_config(log_cb)
        else:
            self.proxy_url = ""
        self.tunnel = TunnelClient(proxy_url=self.proxy_url)
        # прокси уже проверен — браузер не перепроверяет
        self.browser = BrowserManager(
            proxy_url=self.proxy_url,
            proxy_settings=self.proxy_settings,
            log_cb=log_cb,
            proxy_prechecked=True,
        )
        self.searxng_url = searxng_url.rstrip("/")
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    # ==================== НАСТРОЙКИ ПРОКСИ (вкладка «Прокси») ====================

    def _proxy_from_config(self, log_cb=None) -> str:
        """Режим прокси из config.json (proxy_mode/proxy_url/proxy_user/proxy_pass)."""
        try:
            from config_manager import load_config

            cfg = load_config()
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        self.proxy_settings = proxy_env.load_proxy_settings(cfg)
        url, _detail = self._apply_proxy_mode(
            str(cfg.get("proxy_mode", "auto") or "auto"),
            self.proxy_settings.server,
            self.proxy_settings.username,
            self.proxy_settings.password,
            log_cb,
        )
        self._sync_settings_from_url(url)
        return url

    def _apply_proxy_mode(
        self,
        mode: str,
        address: str = "",
        username: str = "",
        password: str = "",
        log_cb=None,
    ) -> tuple[str, str]:
        """Выбор прокси по режиму настроек. Возвращает (адрес, пояснение)."""
        log = log_cb if log_cb is not None else self.log_cb

        def _say(message: str) -> None:
            if log:
                try:
                    log(message)
                except Exception:
                    pass

        if mode == "direct":
            _say("🚫 Прокси: отключён настройками — прямой доступ")
            return "", "прямой доступ (режим «Без прокси»)"

        if mode == "custom":
            if not (address or "").strip():
                _say("⚠️ Прокси: адрес не задан — работает прямой доступ")
                return "", "адрес прокси не задан — работает прямой доступ"
            full_url = proxy_env.assemble_proxy(address, username, password)
            ok, why = proxy_env._http_verify(full_url)
            if ok:
                server, _, _ = proxy_env.parse_proxy_auth(full_url)
                _say(f"🌐 Прокси: используется заданный в настройках {server}")
                return full_url, f"прокси {server} проверен — работает"
            _say(f"⚠️ Прокси из настроек непригоден ({why}) — работает прямой доступ")
            return "", f"прокси не прошёл проверку ({why}) — работает прямой доступ"

        # auto: заданного нет → env KID_PROXY_URL → системный Windows → прямой
        found = proxy_env.resolve_working_proxy("", log)
        if found:
            return found, f"используется прокси {found} (авто)"
        return "", "прокси не найден — прямой доступ (авто)"

    def _sync_settings_from_url(self, url: str = "") -> None:
        """Синхронизирует server/username/password в настройках с фактическим URL."""
        u = (url or getattr(self, "proxy_url", "") or "").strip()
        if not u:
            return
        server, user, passw = proxy_env.parse_proxy_auth(u)
        if server:
            self.proxy_settings.server = server
        if user:
            self.proxy_settings.username = user
        if passw:
            self.proxy_settings.password = passw

    def apply_proxy_settings(
        self,
        mode: str,
        address: str = "",
        username: str = "",
        password: str = "",
        timezone: str = "",
        locale: str = "",
        accept_language: str = "",
        log_cb=None,
    ) -> dict:
        """Применить настройки прокси «на лету» ко всем подсистемам агента."""
        url, detail = self._apply_proxy_mode(mode, address, username, password, log_cb)
        self.proxy_url = url
        if timezone:
            self.proxy_settings.timezone = timezone
        if locale:
            self.proxy_settings.locale = locale
        if accept_language:
            self.proxy_settings.accept_language = accept_language
        self._sync_settings_from_url(url)
        try:
            self.browser.set_proxy(url)
        except Exception:
            pass
        try:
            self.tunnel.set_proxy(url)
        except Exception:
            pass
        return {"proxy": url, "verified": (mode == "direct") or bool(url), "detail": detail}

    # ==================== TTL-КЭШ ====================

    def _cache_get(self, key: str, ttl: float) -> Any | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            stamp, value = entry
            if now - stamp > ttl:
                self._cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, value: Any, ttl: float) -> None:
        now = time.time()
        with self._cache_lock:
            if len(self._cache) >= _CACHE_MAX_ENTRIES:
                # выкидываем самые старые записи
                for old_key in sorted(self._cache, key=lambda k: self._cache[k][0])[:20]:
                    self._cache.pop(old_key, None)
            self._cache[key] = (now, value)

    # ==================== УНИВЕРСАЛЬНЫЙ СКРЕЙПИНГ СТРАНИЦЫ ====================

    def fetch_page_data_sync(self, url: str, timeout: float = 45.0, use_proxy: bool = True) -> dict[str, Any]:
        """Синхронное чтение страницы через ПЕРСИСТЕНТНЫЙ браузер (см. browser.BrowserManager).

        Раньше каждый вызов поднимал новый event loop, ThreadPoolExecutor и Chromium,
        а после чтения закрывал браузер (1-3 с холодного запуска на каждый fetch).
        Теперь браузер живёт в выделенном потоке — запуск происходит один раз.
        """
        if not is_safe_target(url):
            return {"url": url, "error": "Адрес запрещён (частная/локальная сеть)"}
        result = self.browser.fetch_page(url, timeout=timeout, use_proxy=use_proxy)
        error_text = str(result.get("error", "")) if isinstance(result, dict) else ""
        if error_text and self.proxy_url and proxy_env.is_proxy_error(error_text):
            # Прокси умер между запросами: перепроверяем и один раз идём напрямую
            if not proxy_env.proxy_alive(self.proxy_url, timeout=2.0):
                self._disable_proxy(error_text)
                result = self.browser.fetch_page(url, timeout=timeout, use_proxy=False)
        return result

    def _disable_proxy(self, reason: str) -> None:
        """Отключить прокси во всех подсистемах после его сбоя."""
        logger.warning(f"Прокси отключён: {reason}")
        self.proxy_url = ""
        try:
            self.browser.drop_proxy(reason)
        except Exception:
            pass
        try:
            self.tunnel.session.proxies = {}
        except Exception:
            pass

    def _fast_fetch(self, url: str, use_proxy: bool = True) -> dict[str, Any]:
        """БЫСТРЫЙ ПУТЬ: curl_cffi + Trafilatura для статических страниц.

        use_proxy=True — через прокси; False — напрямую. Если очищенного тела
        мало — возвращаем пустой dict, маршрут идёт дальше (браузер).
        """
        if not is_safe_target(url):
            return {}
        proxies = None
        if use_proxy and self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        try:
            resp = requests.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=(8, 12),
                allow_redirects=True,
                stream=True,
                impersonate=IMPERSONATE_TARGET,
            )
            if resp.status_code != 200:
                resp.close()
                return {}
            raw = read_bounded(resp, MAX_HTML_BYTES)
            if raw is None:
                logger.info(f"Быстрый путь: превышен лимит {MAX_HTML_BYTES} байт для {url}")
                return {}
            if not raw:
                return {}
        except Exception as e:
            logger.info(f"Быстрый путь не удался для {url}: {type(e).__name__}")
            return {}

        fallback_encoding = "utf-8"
        m = re.search(r"charset=([\w\-]+)", resp.headers.get("Content-Type", ""), re.I)
        if m:
            fallback_encoding = m.group(1)

        text, title, description, main_image = _extract_content(
            raw, url, encoding=fallback_encoding
        )

        # Единственный критерий успеха — объём очищенного ТЕЛА статьи. Заголовок
        # и Open Graph на решение не влияют: короткий текст всегда уходит в
        # дальнейший каскад (Jina -> Playwright).
        if len(text) < MIN_BODY_CHARS:
            return {}

        return {
            "url": url,
            "title": title,
            "description": description,
            "main_image": main_image,
            "text_content": text[:100000],
            "source_truncated": len(text) > 100000,
            "extracted_chars": len(text),
            "fast_path": True,
        }

    @staticmethod
    def _is_useful(data) -> bool:
        """Есть ли в результате полезное содержимое (не ошибка и не скип)."""
        if not isinstance(data, dict):
            return False
        if data.get("error") or data.get("skipped"):
            return False
        return bool(data.get("text_content") or data.get("description") or data.get("title"))

    def _fetch_route(self, url: str, use_proxy: bool) -> dict[str, Any]:
        """Один маршрут: быстрый путь (Trafilatura) -> браузер (Playwright)."""
        data = self._fast_fetch(url, use_proxy=use_proxy)
        if self._is_useful(data):
            return data
        return self.fetch_page_data_sync(url, use_proxy=use_proxy)

    def fetch_with_fallback(self, url: str) -> dict[str, Any]:
        """Чтение страницы: прокси → напрямую → скип.

        Любой отказ (блок по IP, таймаут, капча, JS-челлендж, пустое тело)
        трактуется одинаково: пробуем второй маршрут; если и он пуст — «пропущен».
        """
        if not is_safe_target(url):
            return {"url": url, "error": "Адрес запрещён (частная/локальная сеть)"}

        # маршрут через прокси (если прокси настроен)
        data = self._fetch_route(url, use_proxy=True) if self.proxy_url else {}
        if not self._is_useful(data):
            # маршрут напрямую
            data = self._fetch_route(url, use_proxy=False)
        if not self._is_useful(data):
            return {
                "url": url,
                "skipped": True,
                "error": "страница не прочитана ни через прокси, ни напрямую",
            }
        return data

    def _get_page_data(self, url: str) -> dict[str, Any]:
        """Данные страницы: кэш → чтение с фолбэком «прокси → напрямую → скип»."""
        if not is_safe_target(url):
            return {"url": url, "error": "Адрес запрещён (частная/локальная сеть)"}
        cache_key = f"page:{url}"
        cached = self._cache_get(cache_key, _CACHE_TTL_PAGES)
        if cached is not None:
            return cached

        data = self.fetch_with_fallback(url)

        if isinstance(data, dict) and not data.get("error") and not data.get("skipped"):
            self._cache_set(cache_key, data, _CACHE_TTL_PAGES)
        return data if isinstance(data, dict) else {}

    def format_page_summary(self, data: dict[str, Any]) -> str:
        """Форматирует собранные данные страницы в чистый блок для контекста нейросети."""
        if not data or data.get("error"):
            err = data.get("error", "Неизвестная ошибка") if data else "Пустой ответ"
            return f"Не удалось открыть страницу ({err})."

        lines = []
        if data.get("title"):
            lines.append(f"📌 **Заголовок:** {data['title']}")
        if data.get("price"):
            curr = data.get("currency") or "RUB"
            lines.append(f"💰 **Цена:** {data['price']} {curr}")
        if data.get("rating"):
            rev = (
                f" (отзывов: {data['reviews_count']})"
                if data.get("reviews_count")
                else ""
            )
            lines.append(f"⭐ **Рейтинг:** {data['rating']}{rev}")
        if data.get("main_image"):
            lines.append(f"🖼 **Фото / Превью:** {data['main_image']}")

        if data.get("specs"):
            lines.append("\n📋 **Характеристики:**")
            for k, v in list(data["specs"].items())[:15]:
                lines.append(f"  • {k}: {v}")

        if data.get("description"):
            lines.append(f"\n📝 **Описание:**\n{data['description']}")
        elif data.get("text_content"):
            snippet = data["text_content"][:1500].strip()
            lines.append(f"\n📄 **Текстовое содержание:**\n{snippet}...")

        lines.append(f"\n🔗 **Источник:** {data.get('url', '')}")
        return "\n".join(lines)

    def download_image_as_data_uri(self, img_url: str) -> str | None:
        """
        Скачивает изображение (напрямую для РФ-зон, через прокси для зарубежных)
        и кодирует в data:image/...;base64 для передачи зрению модели.
        """
        if not img_url or not img_url.startswith("http"):
            return None
        if not is_safe_target(img_url):
            return None

        bypass_proxy = self.browser._should_bypass_proxy(img_url)
        proxies = None
        if not bypass_proxy and self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }

        try:
            resp = requests.get(
                img_url,
                headers=headers,
                proxies=proxies,
                timeout=10,
                stream=True,
                impersonate=IMPERSONATE_TARGET,
            )
            if resp.status_code != 200:
                resp.close()
                return None
            content = read_bounded(resp, MAX_IMAGE_BYTES)
            if content is None:
                logger.warning(f"Картинка превышает {MAX_IMAGE_BYTES} байт: {img_url}")
                return None
            if not content:
                return None
            content_type = (
                resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            )
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            b64_data = base64.b64encode(content).decode("utf-8")
            return f"data:{content_type};base64,{b64_data}"
        except Exception as e:
            logger.warning(
                f"Не удалось скачать изображение {img_url} для передачи зрению: {e}"
            )

        return None

    def fetch_webpage(self, url: str, with_media: bool = False) -> str:
        """Метод для инструмента fetch_webpage: возвращает текстовую сводку.

        with_media=False (по умолчанию): картинка НЕ скачивается — раньше она
        грузилась всегда и съедала до 10 с на страницах с тяжёлыми изображениями,
        хотя текстовому инструменту она не нужна.
        """
        from public_sources import read_public
        public = read_public(url)
        if public is not None:
            if public.get("error"):
                return f"Публичный материал не прочитан ({public['error']}). Источник: {url}"
            from search_tools import format_search_block
            kind = public.get("content_kind", "metadata")
            return format_search_block([{**public, "body": public.get("text_content", ""),
                "read_status": "metadata_only" if kind in ("metadata", "video_description") else "read"}])
        from discussion_reader import read_discussion
        discussion = read_discussion(url)
        if discussion and discussion.get("text_content"):
            from search_tools import format_search_block
            return format_search_block([{**discussion, "body": discussion["text_content"], "read_status": "read"}])
        data = self._get_page_data(url)
        summary = self.format_page_summary(data)
        if with_media:
            img = data.get("main_image")
            if img:
                self.download_image_as_data_uri(img)
        return summary

    # ==================== ПОИСК И АГРЕГАЦИЯ ====================

    def search_and_summarize(
        self, query: str, max_results: int = 5, deep_scrape_top: int = 2,
        *, budget=None, cancel_event=None, progress=None,
    ) -> str:
        """Compatibility entry point using the shared research orchestrator."""
        from search_tools import web_search, format_search_block
        from research_search import SearchBudget

        if budget is None:
            budget = SearchBudget(max_reads=max(0, min(12, int(deep_scrape_top))))
        results, error = web_search(
            query, max_results=max_results, budget=budget,
            cancel_event=cancel_event, progress=progress,
            reader=self._get_page_data, searxng_url=self.searxng_url,
        )
        return format_search_block(results) or error or "Поиск не дал результатов."

    def close(self) -> None:
        """Очистка ресурсов (браузер останавливается через его постоянный цикл)."""
        try:
            self.browser.close()
        except Exception:
            pass
        self.tunnel.close()


agent_core = NetworkAgentCore()
