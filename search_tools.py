# Модуль веб-поиска: Google (Serper API) + Jina Reader (парсинг страниц)
import concurrent.futures
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from config_manager import load_config
from net_guard import MAX_HTML_BYTES, is_safe_target, read_bounded

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080")

_LOG = None


def _log(msg):
    if _LOG:
        try:
            _LOG(msg)
        except Exception:
            pass


def fetch_page_with_jina(url, timeout=6.0, max_chars=3500):
    """
    Заходит по URL через Jina Reader,
    обходит капчи/Cloudflare и возвращает чистый текст страницы в Markdown.
    """
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return ""
    if not is_safe_target(url):
        return ""

    bad_ext = (".pdf", ".jpg", ".png", ".mp4", ".mp3", ".zip", ".exe", ".docx", ".xlsx")
    low_url = url.lower()
    for ext in bad_ext:
        if low_url.endswith(ext):
            return ""

    # Конкатенация URL для Jina Reader
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-No-Cache": "true",
        "X-Return-Format": "markdown",
    }

    try:
        r = requests.get(jina_url, headers=headers, timeout=timeout, stream=True)
        if r.status_code != 200:
            r.close()
            return ""
        raw = read_bounded(r, MAX_HTML_BYTES)
        if raw is None:
            return ""
        text = raw.decode("utf-8", errors="replace").strip()
        # Убираем служебные шапки Jina
        text = re.sub(
            r"^Title:.*?\nURL Source:.*?\nMarkdown Content:\n",
            "",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r"\n\n\n+", "\n\n", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "..."
        return text
    except Exception as ex:
        _log(f"Jina Reader fetch error for {url}: {ex}")

    return ""


def _page_quality(item):
    """Оценивает страницу до дорогостоящего чтения: официальные и содержательные выше SEO-агрегаторов."""
    url = (item.get("url") or "").lower()
    title = (item.get("title") or "").lower()
    snippet = (item.get("body") or "").lower()
    host = urlparse(url).netloc
    score = min(len(snippet) / 160.0, 2.0)
    # Ранжирование учитывает авторитетность, полноту и свежесть результата.
    if item.get("published"):
        try:
            age_days = max((datetime.now(timezone.utc) - datetime.fromisoformat(
                str(item["published"]).replace("Z", "+00:00")
            )).days, 0)
            score += max(0.0, 1.5 - age_days / 365.0)
        except (TypeError, ValueError):
            pass
    score += float(item.get("source_score", 0.0) or 0.0)
    if any(part in host for part in ("wikipedia.org", ".gov", ".edu", ".org")):
        score += 2.0
    if any(word in title for word in ("официальн", "документац", "руководство", "documentation")):
        score += 1.5
    if any(word in host for word in ("pinterest", "youtube", "facebook", "instagram", "tiktok")):
        score -= 2.0
    return score


def _extract_relevant_fragment(text, query, max_chars=8000):
    """Выбирает окна вокруг терминов запроса, а не механически начало документа."""
    if not text:
        return ""
    terms = [term.lower() for term in re.findall(r"[\wА-Яа-яЁё-]{4,}", query or "")]
    lowered = text.lower()
    positions = sorted({lowered.find(term) for term in terms if lowered.find(term) >= 0})
    if not positions:
        return text[:max_chars].rsplit(" ", 1)[0] + ("..." if len(text) > max_chars else "")
    half = max_chars // 2
    start = max(0, positions[0] - half)
    fragment = text[start:start + max_chars]
    if start:
        fragment = "..." + fragment
    if start + max_chars < len(text):
        fragment += "..."
    return fragment


def _enrich_with_jina(results, query="", max_fetch=8):
    """Читает наиболее качественные страницы (прокси → напрямую → скип) и
    сохраняет релевантные фрагменты вместо сниппетов."""
    if not results:
        return results
    targets = sorted(results, key=_page_quality, reverse=True)[:max_fetch]

    def _read(item):
        try:
            # Ленивый импорт: network_agent тянет playwright (тяжёлые библиотеки).
            from network_agent.core import agent_core

            data = agent_core.fetch_with_fallback(item.get("url", ""))
            content = data.get("text_content", "") or data.get("description", "")
            if content:
                item["body"] = _extract_relevant_fragment(content, query)
        except Exception as ex:
            _log(f"Enrichment error for {item.get('url')}: {ex}")
        return item

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_fetch, len(targets))) as executor:
        list(executor.map(_read, targets))
    return results


def _searxng_search(query, max_results, region):
    try:
        response = requests.get(
            f"{SEARXNG_URL.rstrip('/')}/search",
            params={"q": query, "format": "json", "language": region or "ru", "safesearch": 0},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        return [{"title": item.get("title", ""), "url": item.get("url", ""), "body": item.get("content", ""), "date": (item.get("publishedDate") or "").strip()}
                for item in data.get("results", [])[:max_results] if item.get("url")]
    except Exception as ex:
        _log(f"SearXNG search failed: {ex}")
        return []


def should_search(query):
    """Лёгкий локальный классификатор: отделяет справочные/актуальные вопросы от small talk.

    Приоритет — поиску: отсекаем только явный small talk (приветствия/благодарности).
    Любой запрос с отсылкой к внешним событиям, новостям, датам или происшествиям
    обязан получать флаг поиска независимо от вопросительной (в т.ч. субъективной) формы —
    «что думаешь о недавнем инциденте?» требует поиска так же, как и прямой вопрос.
    """
    text = (query or "").strip().lower()
    if not text:
        return False
    if re.fullmatch(r"[\w\s]+[!?.,]*", text) and text in {
        "привет", "здравствуйте", "спасибо", "ок", "понятно", "как дела", "пока"
    }:
        return False
    # Субъективные конструкции («что думаешь», «как относишься», «твоё мнение»)
    # больше не являются поводом отказывать в поиске: при сомнении ищем.
    return True


def _normalize_query(query):
    """Убирает разговорный шум и оставляет поисковую формулировку сложного запроса."""
    text = re.sub(r"^(пожалуйста|можешь|можно|расскажи мне|найди мне)[:,]?\s*", "", query.strip(), flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return text[:300]


def _serper_search(query, max_results, region):
    """Поиск через Serper API — запасной канал на случай недоступности SearXNG."""
    config = load_config()
    search_api_key = config.get("search_api_key") or SERPER_API_KEY
    if not search_api_key or search_api_key == "ВАШ_КЛЮЧ":
        return []
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": search_api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": region[:2] if region else "ru", "hl": region[:2] if region else "ru", "num": max_results},
            timeout=8,
        )
        if response.status_code == 200:
            data = response.json()
            return [{"title": (item.get("title") or link).strip(), "url": link, "body": (item.get("snippet") or "").strip(), "date": (item.get("date") or "").strip()}
                    for item in data.get("organic", [])[:max_results]
                    for link in [(item.get("link") or "").strip()] if link]
    except Exception as ex:
        _log(f"Serper search failed: {ex}")
    return []


def web_search(query, max_results=5, region="ru", backend=None):
    """Поиск через локальный SearXNG, при недоступности — запасной Serper.

    Найденные страницы обогащаются чтением с фолбэком «прокси → напрямую → скип».
    """
    query = _normalize_query(query or "")
    if not query:
        return [], "пустой запрос"
    # Классификатор теперь отсекает ТОЛЬКО явный small talk (приветствия,
    # благодарности): при малейшем сомнении поиск выполняется. Субъективные и
    # вопросительные формы («что думаешь», «как относишься») поиск не блокируют.
    if not should_search(query):
        return [], "Классификатор: поиск для этого запроса не требуется"

    results = _searxng_search(query, max_results, region)
    if not results:
        _log("SearXNG недоступен, переключаюсь на Serper")
        results = _serper_search(query, max_results, region)
    if not results:
        return [], "По запросу ничего не найдено: SearXNG и Serper недоступны"
    _log("Чтение качественных страниц и извлечение релевантных фрагментов...")
    return _enrich_with_jina(results, query=query, max_fetch=min(8, len(results))), None


def _source_type(url):
    """Короткая метка типа источника по домену — помогает модели взвешивать достоверность."""
    host = urlparse(url or "").netloc.lower()
    if not host:
        return ""
    if "wikipedia" in host:
        return "вики"
    if host.endswith((".gov", ".edu")) or host.endswith(".gov.ru"):
        return "официальный"
    if any(w in host for w in ("benchlm", "lmarena", "huggingface", "leaderboard", "benchmark", "lmsys")):
        return "бенчмарк"
    if any(w in host for w in ("github", "gitlab", "docs.", "documentation", "developer.", "readthedocs")):
        return "документация"
    if any(w in host for w in ("habr", "vc.ru", "medium", "dev.to", "dzen", "blog", "tjournal")):
        return "статья/блог"
    return ""


def format_search_block(results):
    """Формирует структурированный блок для модели с чистым содержимым страниц."""
    if not results:
        return ""
    blocks = []
    for i, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        body = (item.get("body") or "").strip()
        date = (item.get("date") or "").strip()
        stype = _source_type(url)
        if not body and not title:
            continue

        meta = []
        if stype:
            meta.append(stype)
        if date:
            meta.append(date)
        lines = [f"[{i}] {title}"]
        if meta:
            lines.append(" | ".join(meta))
        if url:
            lines.append(f"URL: {url}")
        if body:
            lines.append(f"Содержимое:\n{body}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return "Информация из интернета:\n" + "\n\n---\n\n".join(blocks) + "\n\n"
