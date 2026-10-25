"""Локальный поиск, проверка источников и метрики качества retrieval."""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app_paths import USER_DATA_DIR

ROOT = USER_DATA_DIR
METRICS_PATH = ROOT / "retrieval_metrics.json"
_LOCK = threading.Lock()


def _load_metrics():
    try:
        return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"requests": 0, "successes": 0, "latency_ms": [], "cost": 0.0, "citations": 0}


def record_request(success, latency_ms, cost=0.0, citations=0):
    with _LOCK:
        data = _load_metrics()
        data["requests"] += 1
        data["successes"] += int(bool(success))
        data["latency_ms"] = (data.get("latency_ms", []) + [round(latency_ms, 2)])[-200:]
        data["cost"] = round(float(data.get("cost", 0.0)) + float(cost or 0), 6)
        data["citations"] = int(data.get("citations", 0)) + int(citations or 0)
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        METRICS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _files():
    notes_file = ROOT / "notes.json"
    if notes_file.is_file():
        yield notes_file
    roots = [ROOT / "history", ROOT / "generated_files", ROOT / "notes", ROOT / "documents"]
    allowed = {".txt", ".md", ".json", ".csv", ".py"}
    for base in roots:
        if base.exists():
            for path in base.rglob("*"):
                if path.is_file() and path.suffix.lower() in allowed and path.stat().st_size <= 2_000_000:
                    yield path


def local_search(query, max_results=5):
    terms = [x.lower() for x in re.findall(r"[\wА-Яа-яЁё-]{3,}", query or "")]
    if not terms:
        return []
    results = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = text.lower()
        score = sum(lowered.count(term) for term in terms)
        if score:
            pos = min((lowered.find(term) for term in terms if lowered.find(term) >= 0), default=0)
            body = text[max(0, pos - 250):pos + 1800].strip()
            results.append({"title": path.name, "url": str(path.relative_to(ROOT)), "body": body, "source": "local", "score": score})
    return sorted(results, key=lambda item: item["score"], reverse=True)[:max_results]


def verify_claims(results, query=""):
    """Сохраняет найденные материалы без проверки независимых источников."""
    return results


def strict_citations(results):
    return ["[%d] %s — %s" % (index, item.get("title", "Источник"), item.get("url", "")) for index, item in enumerate(results, 1) if item.get("url")]


def format_local_results(results):
    results = verify_claims(results)
    if not results:
        return "По локальным документам ничего не найдено."
    blocks = [
        "[%d] %s\nФайл: %s\nФрагмент:\n%s"
        % (index, item.get("title", "Документ"), item.get("url", ""), item.get("body", ""))
        for index, item in enumerate(results, 1)
    ]
    return "Локальные источники:\n" + "\n\n---\n\n".join(blocks) + "\n\nСтрогие цитаты:\n" + "\n".join(strict_citations(results))
