"""
Сетевой шлюз (туннель) для внешних API:
- SearXNG (поисковый агрегатор)
- Jina Reader (быстрое извлечение Markdown из легковесных страниц)
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import requests

from net_guard import MAX_HTML_BYTES, is_safe_target, read_bounded

logger = logging.getLogger("NetworkAgent.Tunnel")


class TunnelClient:
    def __init__(self, proxy_url: str = ""):
        self.proxy_url = proxy_url
        self.session = requests.Session()
        # «Прямой доступ» должен быть действительно прямым: без этого requests
        # подхватил бы переменные окружения и отправил трафик в мёртвый прокси.
        self.session.trust_env = False

        if self.proxy_url:
            self.session.proxies = {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }

        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )

    def set_proxy(self, proxy_url: str) -> None:
        """Сменить прокси на лету (вкладка «Прокси» настроек)."""
        self.proxy_url = proxy_url or ""
        if self.proxy_url:
            self.session.proxies = {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }
        else:
            self.session.proxies = {}

    # ==================== ПОИСК ЧЕРЕЗ SEARXNG ====================

    def searxng_search(
        self,
        query: str,
        searxng_url: str = "http://127.0.0.1:8080",
        categories: str = "general",
        language: str = "ru",
        time_range: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Запрос к локальному или удаленному инстансу SearXNG.
        Возвращает структурированный список результатов.
        """
        # ЯВНОЕ ИСКЛЮЧЕНИЕ SSRF-гейта: адрес SearXNG приходит из конфигурации/env,
        # а не от модели, поэтому net_guard.is_safe_target здесь намеренно не
        # вызывается — локальный 127.0.0.1:8080 обязан ходить напрямую.
        url = f"{searxng_url.rstrip('/')}/search"
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "categories": categories,
            "language": language,
        }
        if time_range:
            params["time_range"] = time_range

        try:
            # Для обращения к локальному SearXNG (127.0.0.1:8080) прокси не нужен
            is_local = "127.0.0.1" in searxng_url or "localhost" in searxng_url
            proxies = None if is_local else self.session.proxies

            resp = requests.get(
                url,
                params=params,
                headers={"Accept": "application/json"},
                proxies=proxies,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            logger.info(
                f"SearXNG вернул {len(results)} результатов по запросу: {query}"
            )
            return results

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса к SearXNG ({searxng_url}): {e}")
            return []
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при разборе ответа SearXNG: {e}")
            return []

    # ==================== БЫСТРОЕ ЧТЕНИЕ ЧЕРЕЗ JINA READER ====================

    def jina_read(self, target_url: str) -> str | None:
        """
        Быстрое чтение страницы через облачный сервис Jina Reader (https://r.jina.ai/).
        Возвращает очищенный текст/markdown или None, если ресурс заблокирован/недоступен.
        """
        if not is_safe_target(target_url):
            return None
        encoded_url = quote(target_url, safe=":/%?&=#+-_")
        jina_endpoint = f"https://r.jina.ai/{encoded_url}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-With-Generated-Alt": "true",
        }

        try:
            resp = self.session.get(jina_endpoint, headers=headers, timeout=8, stream=True)
            if resp.status_code != 200:
                logger.warning(
                    f"Jina вернула статус {resp.status_code} для {target_url}"
                )
                resp.close()
                return None

            raw = read_bounded(resp, MAX_HTML_BYTES)
            if raw is None:
                logger.warning(f"Jina превысила лимит {MAX_HTML_BYTES} байт для {target_url}")
                return None

            text = raw.decode("utf-8", errors="replace").strip()

            # Проверка на заглушки Cloudflare, блокировки и капчи
            lower_text = text.lower()
            block_markers = [
                "just a moment...",
                "enable javascript and cookies to continue",
                "attention required! | cloudflare",
                "captcha",
                "доступ ограничен",
                "blocked",
            ]
            if any(marker in lower_text for marker in block_markers):
                logger.warning(f"Jina получила страницу блокировки для {target_url}")
                return None

            return text if len(text) > 50 else None

        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут чтения через Jina: {target_url}")
            return None
        except Exception as e:
            logger.warning(f"Ошибка чтения через Jina Reader: {e}")
            return None

    def close(self) -> None:
        """Закрытие сессии."""
        self.session.close()
