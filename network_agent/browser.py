"""
Playwright-браузер для чтения веб-страниц, SPA-приложений, маркетплейсов и медиа.
Автоматически определяет зону .ru / .рф для прямого доступа без VPN.

Оптимизации скорости (инструменты модели вызывают это на каждый fetch):
- браузер запускается ОДИН раз и живёт в выделенном event-loop-потоке:
  объекты Playwright нельзя использовать из чужого цикла, поэтому все
  операции идут через run_coroutine_threadsafe в этот же поток;
- контексты (прямой доступ / через прокси) создаются один раз и переиспользуются;
- картинки/шрифты/видео блокируются на уровне контекста: текст и атрибуты
  картинок остаются в DOM, а трафик не качается (2-5x на тяжёлых страницах);
- вместо фиксированных пауз 2000+800мс — ожидание networkidle с коротким
  лимитом: страница готова, как только затихла сеть.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, async_playwright

from net_guard import is_safe_target

from .proxy_env import ProxySettings, parse_proxy_auth, resolve_working_proxy

logger = logging.getLogger("NetworkAgent.Browser")

# Только российские сервисы с не-RU доменами, которые режут зарубежные IP
DIRECT_NON_RU_DOMAINS = {
    "vk.com",
    "vkvideo.ru",
    "t.me",
}

# Ресурсы, не нужные для извлечения текста/карточки товара (атрибуты остаются в DOM)
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})

# Короткий запас после networkidle — дать JS-фреймворкам дорисовать DOM
SETTLE_MS = 400

# Скрытие следов автоматизации: убираем navigator.webdriver, эмулируем chrome.runtime
# и объекты, которые проверяют антибот-системы. Внедряется в каждый контекст.
STEALTH_INIT_SCRIPT = """
(() => {
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    } catch (e) {}
    try {
        delete Object.getPrototypeOf(navigator).webdriver;
    } catch (e) {}

    if (!window.chrome) {
        window.chrome = { runtime: {} };
    } else if (!window.chrome.runtime) {
        window.chrome.runtime = {};
    }
    if (!window.chrome.csi) {
        window.chrome.csi = () => ({ startE: Date.now(), onloadT: Date.now(), pageT: 120, tran: 15 });
    }
    if (!window.chrome.loadTimes) {
        window.chrome.loadTimes = () => ({
            requestTime: Date.now() / 1000, startLoadTime: Date.now() / 1000,
            commitLoadTime: Date.now() / 1000, finishDocumentLoadTime: Date.now() / 1000,
            finishLoadTime: Date.now() / 1000, firstPaintTime: Date.now() / 1000,
            navigationType: 'Other', wasAlternateProtocolAvailable: false,
            connectionInfo: 'h2', npnNegotiatedProtocol: 'h2',
            wasFetchedViaSpdy: true, wasNpnNegotiated: true,
        });
    }
    if (!navigator.chrome) {
        try { Object.defineProperty(navigator, 'chrome', { get: () => window.chrome }); } catch (e) {}
    }
    if (!navigator.languages) {
        try { Object.defineProperty(navigator, 'languages', { get: () => ['et-EE', 'et', 'ru-RU', 'en-US'] }); } catch (e) {}
    }
    try {
        if (!navigator.plugins || navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        }
    } catch (e) {}
})();
"""


async def _block_heavy_resources(route) -> None:
    try:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
    except Exception:
        pass
    try:
        await route.continue_()
    except Exception:
        pass


class BrowserManager:
    """Playwright-ядро с ПЕРСИСТЕНТНЫМ циклом событий и переиспользуемыми контекстами."""

    def __init__(
        self,
        proxy_url: str = "",
        page_timeout_ms: int = 30000,
        log_cb=None,
        proxy_prechecked: bool = False,
        proxy_settings: ProxySettings | None = None,
    ):
        self.proxy_url = proxy_url
        self.page_timeout_ms = page_timeout_ms
        self._log_cb = log_cb
        self._proxy_prechecked = proxy_prechecked
        self.proxy_settings = proxy_settings or ProxySettings()
        self._pw = None
        self._browser = None
        self._contexts: dict[bool, BrowserContext] = {}
        self._lock = asyncio.Lock()

        # Выделенный поток с постоянным event loop: Playwright-объекты
        # создаются и используются ТОЛЬКО в нём (требование asyncio API).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.RLock()
        self._closed = False

    # ---------- постоянный event loop ----------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._closed:
                raise RuntimeError("Браузерное ядро уже остановлено")
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                self._loop_thread = threading.Thread(
                    target=self._loop.run_forever,
                    name="NetworkAgent-BrowserLoop",
                    daemon=True,
                )
                self._loop_thread.start()
            return self._loop

    def _run_coro(self, coro, timeout: float):
        """Выполнить корутину в постоянном цикле с общим таймаутом."""
        try:
            with self._loop_lock:
                loop = self._ensure_loop()
                future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            raise
        try:
            return future.result(timeout=timeout + 2)
        except Exception:
            future.cancel()
            raise

    # ---------- инициализация браузера и контекстов ----------

    async def _ainit_browser(self) -> None:
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return
            # Прокси проверяется ОДИН раз при первом запуске браузера: адрес,
            # который никто не слушает, давал net::ERR_PROXY_CONNECTION_FAILED
            # на каждой зарубежной странице (fetch_webpage/web_search).
            if not self._proxy_prechecked:
                self.proxy_url = resolve_working_proxy(self.proxy_url, self._log_cb)
                self._proxy_prechecked = True
            logger.info("Запуск браузерного ядра Playwright (постоянный)...")
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                # direct:// гарантирует: Chromium НЕ подхватывает системный
                # прокси Windows (реальный источник 407 Proxy Authentication
                # Required и net::ERR_PROXY_CONNECTION_FAILED). Какой прокси
                # использовать, решает каждый контекст сам (см. _aget_context).
                args=[
                    "--proxy-server=direct://",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                ],
            )
            logger.info("Браузерное ядро Playwright запущено (живёт между запросами).")

    def _should_bypass_proxy(self, url: str) -> bool:
        """РФ-домены и локальные адреса открываем напрямую с домашнего IP."""
        try:
            host = (urlparse(url).hostname or "").lower()
            # loopback через прокси не ходит никогда (и ломается о чужие прокси)
            if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
                return True
            if host.endswith((".ru", ".рф", ".su")):
                return True
            if "yandex." in host:
                return True
            return any(
                host == d or host.endswith("." + d) for d in DIRECT_NON_RU_DOMAINS
            )
        except Exception:
            return False

    async def _aget_context(self, bypass_proxy: bool) -> BrowserContext:
        await self._ainit_browser()
        assert self._browser is not None

        cached = self._contexts.get(bypass_proxy)
        if cached is not None:
            try:
                if cached.browser and cached.browser.is_connected():
                    return cached
            except Exception:
                pass
            self._contexts.pop(bypass_proxy, None)

        proxy_config = None
        if not bypass_proxy and self.proxy_url:
            server, username, password = parse_proxy_auth(self.proxy_url)
            proxy_config = {
                "server": server,
                # loopback всегда напрямую, даже в прокси-контексте
                "bypass": "localhost,127.0.0.1",
            }
            if username:
                proxy_config["username"] = username
            if password:
                proxy_config["password"] = password

        # Профиль браузера: через прокси — таймзона/локаль/Accept-Language строго из
        # конфигурации; напрямую — системные дефолты (таймзону не подменяем).
        context_kwargs: dict[str, Any] = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080},
        }
        use_proxy = bool(proxy_config)
        if use_proxy:
            context_kwargs["proxy"] = proxy_config
            settings = self.proxy_settings
            context_kwargs["timezone_id"] = settings.timezone
            context_kwargs["locale"] = settings.locale
            context_kwargs["extra_http_headers"] = {"Accept-Language": settings.accept_language}

        context = await self._browser.new_context(**context_kwargs)
        try:
            await context.route("**/*", _block_heavy_resources)
        except Exception as e:
            logger.warning(f"Не удалось поставить блокировку ресурсов: {e}")
        # Скрытие следов автоматизации (navigator.webdriver, chrome.runtime).
        try:
            await context.add_init_script(STEALTH_INIT_SCRIPT)
        except Exception as e:
            logger.warning(f"Не удалось внедрить stealth-скрипт: {e}")
        self._contexts[bypass_proxy] = context
        return context

    # <<CHUNK2>>

    # ---------- чтение страницы ----------

    async def _afetch_page(self, url: str, wait_selector: str | None = None, use_proxy: bool = True) -> dict[str, Any]:
        bypass_proxy = not use_proxy
        context = await self._aget_context(bypass_proxy=bypass_proxy)
        page = await context.new_page()

        try:
            logger.info(f"Открытие URL: {url} (Напрямую без VPN: {bypass_proxy})")
            # Провайдер/сеть периодически «флапают»: одна попытка с длинным
            # таймаутом сгорала целиком (30с) и роняла fetch. Две короткие
            # попытки по 15с: первая может попасть на флап, вторая — успеть.
            per_attempt_timeout = max(10000, self.page_timeout_ms // 2)
            last_error: Exception | None = None
            for attempt in range(2):
                if attempt > 0:
                    # застрявшая страница бесполезна — открываем заново
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = await context.new_page()
                    await asyncio.sleep(0.8)
                try:
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=per_attempt_timeout
                    )
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"Попытка {attempt + 1}/2 загрузки {url} не удалась: {e}"
                    )
            if last_error is not None:
                raise last_error

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=5000)
                except Exception:
                    pass
            else:
                # Ждём, пока сеть затихнет (но не дольше 2.5 с), затем короткий запас.
                # Фиксированных пауз больше нет: страница готова, как только сеть тише.
                try:
                    await page.wait_for_load_state("networkidle", timeout=2500)
                except Exception:
                    pass
                await page.wait_for_timeout(SETTLE_MS)

            # Защита от пустых SPA: короткий защитный таймаут (до 3 с) на появление
            # семантических контейнеров статьи — динамика успевает отрисоваться.
            try:
                await page.wait_for_selector(
                    "article, main, [role='main'], p, h1, h2, .article, .post, .content, .entry-content",
                    timeout=3000,
                )
            except Exception:
                pass

            extracted_data = await page.evaluate(UNIVERSAL_EXTRACTOR_JS)
            extracted_data["url"] = url
            extracted_data["bypassed_proxy"] = bypass_proxy

            return extracted_data

        except Exception as e:
            logger.error(f"Ошибка загрузки страницы {url}: {e}")
            return {
                "url": url,
                "error": str(e),
                "title": "",
                "description": "",
                "main_image": "",
                "text_content": "",
            }
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ---------- публичный синхронный API ----------

    def fetch_page(
        self, url: str, wait_selector: str | None = None, timeout: float = 45.0,
        use_proxy: bool = True,
    ) -> dict[str, Any]:
        """Синхронное чтение страницы: корутина исполняется в постоянном цикле,
        поэтому браузер НЕ перезапускается между вызовами."""
        if not is_safe_target(url):
            return {
                "url": url,
                "error": "Адрес запрещён (частная/локальная сеть)",
                "title": "",
                "description": "",
                "main_image": "",
                "text_content": "",
            }
        try:
            return self._run_coro(self._afetch_page(url, wait_selector, use_proxy), timeout)
        except Exception as e:
            logger.error(f"Ошибка скрейпинга {url}: {e}")
            return {
                "url": url,
                "error": str(e),
                "title": "",
                "description": "",
                "main_image": "",
                "text_content": "",
            }

    def set_proxy(self, proxy_url: str) -> None:
        """Сменить прокси на лету: пересоздать контексты под новый адрес.

        Вызывается из вкладки «Прокси» настроек и при самолечении после сбоя.
        """
        with self._loop_lock:
            if self._closed:
                return
            self.proxy_url = proxy_url or ""
            self._proxy_prechecked = True
            loop = self._loop
            future = None
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._aclose_all_contexts(), loop)
        if future is not None:
            try:
                future.result(timeout=15)
            except Exception:
                pass

    def drop_proxy(self, reason: str = "") -> None:
        """Прокси отвалился посреди сессии: перейти на прямой доступ."""
        if not self.proxy_url:
            return
        logger.warning(f"Прокси недоступен ({reason}); переключаюсь на прямой доступ.")
        if self._log_cb:
            try:
                self._log_cb("⚠️ Прокси недоступен — переключаюсь на прямой доступ")
            except Exception:
                pass
        self.set_proxy("")

    async def _aclose_all_contexts(self) -> None:
        for context in list(self._contexts.values()):
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()

    def close(self) -> None:
        """Синхронная остановка браузера (shutdown — через его постоянный цикл)."""
        with self._loop_lock:
            if self._closed:
                return
            self._closed = True
            loop, loop_thread = self._loop, self._loop_thread
        if loop is None or loop.is_closed():
            return
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(self._ashutdown(), loop)
            future.result(timeout=15)
        except Exception:
            if future is not None:
                future.cancel()
        finally:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            if loop_thread and loop_thread is not threading.current_thread():
                loop_thread.join(timeout=2)
            if not loop.is_running():
                loop.close()

    async def _ashutdown(self) -> None:
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("Браузерное ядро остановлено.")


UNIVERSAL_EXTRACTOR_JS = """
() => {
    const result = {
        title: "",
        description: "",
        main_image: "",
        price: null,
        currency: null,
        rating: null,
        reviews_count: null,
        specs: {},
        raw_schema: []
    };

    // 1. Извлечение JSON-LD (Schema.org)
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    ldScripts.forEach(script => {
        try {
            const data = JSON.parse(script.innerText.trim());
            const items = Array.isArray(data) ? data : (data['@graph'] ? data['@graph'] : [data]);

            for (const item of items) {
                if (!item || !item['@type']) continue;
                result.raw_schema.push(item);
                const type = String(item['@type']).toLowerCase();

                // Продукт (Ozon, WB, магазины одежды)
                if (type.includes('product')) {
                    if (item.name && !result.title) result.title = item.name;
                    if (item.description && !result.description) result.description = item.description;
                    if (item.image) {
                        const img = Array.isArray(item.image) ? item.image[0] : (item.image.url || item.image);
                        if (typeof img === 'string') result.main_image = img;
                    }
                    if (item.offers) {
                        const offer = Array.isArray(item.offers) ? item.offers[0] : item.offers;
                        if (offer.price) result.price = String(offer.price);
                        if (offer.priceCurrency) result.currency = offer.priceCurrency;
                    }
                    if (item.aggregateRating) {
                        result.rating = item.aggregateRating.ratingValue;
                        result.reviews_count = item.aggregateRating.reviewCount;
                    }
                }

                // Видео (YouTube, Rutube, VK Видео)
                if (type.includes('videoobject')) {
                    if (item.name && !result.title) result.title = item.name;
                    if (item.description && !result.description) result.description = item.description;
                    if (item.thumbnailUrl && !result.main_image) {
                        const thumb = Array.isArray(item.thumbnailUrl) ? item.thumbnailUrl[0] : item.thumbnailUrl;
                        result.main_image = thumb;
                    }
                }
            }
        } catch (e) {}
    });

    // 2. OpenGraph & Twitter Cards
    const getMeta = (props) => {
        for (const prop of props) {
            const el = document.querySelector(`meta[property="${prop}"], meta[name="${prop}"]`);
            if (el && el.content) return el.content.trim();
        }
        return "";
    };

    if (!result.title) result.title = getMeta(['og:title', 'twitter:title']) || document.title;
    if (!result.description) result.description = getMeta(['og:description', 'twitter:description', 'description']);
    if (!result.main_image) result.main_image = getMeta(['og:image', 'twitter:image', 'twitter:image:src']);

    if (!result.price) {
        const ogPrice = getMeta(['product:price:amount', 'og:price:amount']);
        if (ogPrice) {
            result.price = ogPrice;
            result.currency = getMeta(['product:price:currency', 'og:price:currency']) || 'RUB';
        }
    }

    // 3. Эвристический сбор цены, если JSON-LD и OG пусты
    if (!result.price) {
        const priceEl = document.querySelector('[itemprop="price"], [data-widget="webPrice"]');
        if (priceEl) {
            const text = priceEl.innerText || priceEl.getAttribute('content') || '';
            const match = text.replace(/\\s/g, '').match(/\\d+([.,]\\d+)?/);
            if (match) result.price = match[0];
        }
    }

    // 4. Сбор характеристик (Таблицы, dl списки)
    const dlElements = document.querySelectorAll('dl');
    dlElements.forEach(dl => {
        const dts = dl.querySelectorAll('dt');
        const dds = dl.querySelectorAll('dd');
        for (let i = 0; i < Math.min(dts.length, dds.length); i++) {
            const k = dts[i].innerText.trim();
            const v = dds[i].innerText.trim();
            if (k && v && Object.keys(result.specs).length < 25) {
                result.specs[k] = v;
            }
        }
    });

    // 5. Очистка основного текста страницы
    const clones = document.body.cloneNode(true);
    ['script', 'style', 'noscript', 'svg', 'iframe', 'header', 'footer', 'nav'].forEach(tag => {
        clones.querySelectorAll(tag).forEach(el => el.remove());
    });
    const extractedText = (clones.innerText || "").replace(/\\n{3,}/g, '\\n\\n').trim();
    result.text_content = extractedText.slice(0, 100000);
    result.source_truncated = extractedText.length > 100000;
    result.extracted_chars = extractedText.length;

    return result;
}
"""
