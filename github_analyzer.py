"""Автоматический анализ GitHub-репозиториев.

Модуль собирает README, структуру верхнего уровня и манифесты зависимостей
(requirements.txt, package.json, pyproject.toml и аналоги) через GitHub API с
фолбэком на скачивание tarball-архива, после чего формирует через LLM сжатую
техническую справку: назначение, стек, свежесть зависимостей, легаси и вердикт
о целесообразности использования.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
from urllib.parse import urlparse

import requests

GITHUB_API = "https://api.github.com"
RAW_HOST = "https://raw.githubusercontent.com"

# Манифесты зависимостей, которые собираются для анализа свежести/легаси.
MANIFEST_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "composer.json",
    "composer.lock",
    "Gemfile",
    "Gemfile.lock",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
)

ANALYSIS_SYSTEM_PROMPT = (
    "Ты — инженер, анализирующий GitHub-репозитории по собранным метаданным "
    "(README, структура верхнего уровня, манифесты зависимостей). Составь сжатую "
    "техническую справку строго по пунктам:\n"
    "1. Назначение проекта — 1-2 предложения.\n"
    "2. Используемый стек — ключевые технологии и фреймворки.\n"
    "3. Свежесть зависимостей — оценка по манифестам и дате последнего push.\n"
    "4. Наличие легаси — признаки устаревшего или заброшенного кода.\n"
    "5. Вердикт о целесообразности использования — рекомендация с обоснованием.\n"
    "Отвечай по-русски, кратко и по делу, без воды и без дополнительных разделов."
)

_README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme", "readme.markdown")


def parse_repo_url(url: str):
    """Извлекает (owner, repo) из ссылки на GitHub-репозиторий.

    Поддерживает: https://github.com/owner/repo[.git],
    git@github.com:owner/repo.git, github.com/owner/repo, owner/repo.
    """
    if not url or not isinstance(url, str):
        return None
    s = url.strip()

    m = re.match(r"^git@github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", s)
    if m:
        owner, repo = m.group(1).strip("/"), m.group(2).strip("/")
        return (owner, repo) if (owner and repo) else None

    if "://" in s:
        parsed = urlparse(s)
        if parsed.netloc not in ("github.com", "www.github.com"):
            return None
        s = parsed.path

    s = s.strip("/")
    lower = s.lower()
    for host in ("www.github.com/", "github.com/"):
        if lower.startswith(host):
            s = s[len(host):]
            break
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        owner = parts[0]
        repo = re.sub(r"\.git$", "", parts[1])
        if owner and repo:
            return owner, repo
    return None


def _token() -> str:
    """GitHub-токен: env GITHUB_TOKEN либо поле config.json (github_token)."""
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env
    try:
        from config_manager import load_config

        return str(load_config().get("github_token") or "").strip()
    except Exception:
        return ""


def _api_get(session, url: str, token: str, accept: str = "application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": "kid-repo-analyzer"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = session.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp


def _fetch_raw(session, owner: str, repo: str, branch: str, path: str) -> str | None:
    url = f"{RAW_HOST}/{owner}/{repo}/{branch}/{path}"
    try:
        resp = session.get(url, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.text or ""


def _collect_via_api(owner: str, repo: str, token: str) -> dict:
    meta: dict = {
        "owner": owner,
        "repo": repo,
        "default_branch": "main",
        "description": "",
        "language": "",
        "license": "",
        "stars": None,
        "pushed_at": "",
        "archived": False,
        "fork": False,
        "readme": "",
        "top_level": [],
        "manifests": {},
        "errors": [],
    }
    session = requests.Session()
    session.trust_env = False

    info = _api_get(session, f"{GITHUB_API}/repos/{owner}/{repo}", token)
    if info is not None:
        data = info.json()
        meta["default_branch"] = data.get("default_branch") or "main"
        meta["description"] = data.get("description") or ""
        meta["language"] = data.get("language") or ""
        meta["stars"] = data.get("stargazers_count")
        meta["pushed_at"] = data.get("pushed_at") or ""
        meta["archived"] = bool(data.get("archived"))
        meta["fork"] = bool(data.get("fork"))
        license_info = data.get("license") or {}
        meta["license"] = license_info.get("spdx_id") or ""
    else:
        meta["errors"].append("метаданные репозитория недоступны (возможен rate-limit)")

    listing = _api_get(session, f"{GITHUB_API}/repos/{owner}/{repo}/contents/", token)
    if listing is not None:
        try:
            for entry in listing.json():
                if isinstance(entry, dict):
                    meta["top_level"].append(
                        {"name": entry.get("name"), "type": entry.get("type")}
                    )
        except ValueError:
            meta["errors"].append("не удалось разобрать структуру верхнего уровня")
    else:
        meta["errors"].append("структура верхнего уровня недоступна")

    readme_resp = _api_get(
        session,
        f"{GITHUB_API}/repos/{owner}/{repo}/readme",
        token,
        accept="application/vnd.github.raw+json",
    )
    if readme_resp is not None:
        meta["readme"] = (readme_resp.text or "")[:16000]
    else:
        readme_file = next(
            (e["name"] for e in meta["top_level"] if e["name"].lower() in _README_NAMES),
            None,
        )
        if readme_file:
            raw = _fetch_raw(session, owner, repo, meta["default_branch"], readme_file)
            meta["readme"] = (raw or "")[:16000] if raw else ""
        if not meta["readme"]:
            meta["errors"].append("README не найден")

    root_files = {e["name"] for e in meta["top_level"]}
    for fname in MANIFEST_FILES:
        if fname in root_files:
            raw = _fetch_raw(session, owner, repo, meta["default_branch"], fname)
            if raw:
                meta["manifests"][fname] = raw[:8000]

    return meta


def _collect_via_archive(owner: str, repo: str, branch: str) -> dict:
    """Фолбэк: скачиваем tarball с codeload и читаем файлы напрямую (без API)."""
    meta: dict = {
        "owner": owner,
        "repo": repo,
        "default_branch": branch,
        "description": "",
        "language": "",
        "license": "",
        "stars": None,
        "pushed_at": "",
        "archived": False,
        "fork": False,
        "readme": "",
        "top_level": [],
        "manifests": {},
        "errors": [],
    }
    session = requests.Session()
    session.trust_env = False

    raw = None
    for candidate in (branch, "main", "master"):
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{candidate}"
        try:
            resp = session.get(url, timeout=40)
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            raw = resp.content
            meta["default_branch"] = candidate
            break
    if raw is None:
        meta["errors"].append("не удалось скачать архив репозитория")
        return meta

    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        meta["errors"].append(f"не удалось распаковать архив: {exc}")
        return meta

    names = tf.getnames()
    prefix = names[0].split("/")[0] if names else ""
    top: set[str] = set()
    try:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            rel = member.name[len(prefix) + 1 :] if prefix else member.name
            parts = rel.split("/")
            if parts:
                top.add(parts[0])
            if "/" not in rel:
                base = parts[-1].lower()
                if base in _README_NAMES and not meta["readme"]:
                    try:
                        meta["readme"] = tf.extractfile(member).read().decode("utf-8", "replace")[:16000]
                    except Exception:
                        pass
                elif parts[-1] in MANIFEST_FILES:
                    try:
                        meta["manifests"][parts[-1]] = tf.extractfile(member).read().decode("utf-8", "replace")[:8000]
                    except Exception:
                        pass
    finally:
        tf.close()

    meta["top_level"] = [{"name": n, "type": "dir"} for n in sorted(top)]
    if not meta["readme"]:
        meta["errors"].append("README не найден в архиве")
    return meta


def collect_metadata(owner: str, repo: str, token: str | None = None) -> dict:
    """Собирает метаданные: API в первую очередь, архив — как фолбэк."""
    token = token or _token()
    meta = _collect_via_api(owner, repo, token)
    if meta.get("top_level") or meta.get("manifests") or meta.get("readme"):
        return meta
    return _collect_via_archive(owner, repo, meta.get("default_branch") or "main")


def build_analysis_prompt(meta: dict) -> str:
    """Формирует текст-контекст для LLM из собранных метаданных."""
    lines: list[str] = [f"Репозиторий: {meta.get('owner')}/{meta.get('repo')}"]
    if meta.get("description"):
        lines.append(f"Описание: {meta['description']}")
    if meta.get("language"):
        lines.append(f"Основной язык: {meta['language']}")
    if meta.get("license"):
        lines.append(f"Лицензия: {meta['license']}")
    if meta.get("stars") is not None:
        lines.append(f"Звёзд: {meta['stars']}")
    if meta.get("pushed_at"):
        lines.append(f"Последний push: {meta['pushed_at']}")
    if meta.get("default_branch"):
        lines.append(f"Ветка по умолчанию: {meta['default_branch']}")
    flags = []
    if meta.get("archived"):
        flags.append("АРХИВИРОВАН")
    if meta.get("fork"):
        flags.append("FORK")
    if flags:
        lines.append(f"Статус: {', '.join(flags)}")

    lines.append("\nСтруктура верхнего уровня:")
    if meta.get("top_level"):
        for entry in meta["top_level"]:
            marker = "[dir] " if entry.get("type") == "dir" else "[file]"
            lines.append(f"  {marker}{entry.get('name')}")
    else:
        lines.append("  (недоступна)")

    lines.append("\nМанифесты зависимостей:")
    manifests = meta.get("manifests") or {}
    if manifests:
        for name, content in manifests.items():
            lines.append(f"\n--- {name} ---\n{content}")
    else:
        lines.append("  (манифесты не найдены)")

    readme = (meta.get("readme") or "").strip()
    lines.append(f"\nREADME (фрагмент):\n{readme[:10000] if readme else '(недоступен)'}")

    if meta.get("errors"):
        lines.append("\nЗамечания при сборе: " + "; ".join(meta["errors"]))

    return "\n".join(lines)


def analyze_repository(
    url: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float = 0.3,
    timeout: int = 120,
    token: str | None = None,
) -> str:
    """Полный цикл: сбор метаданных -> LLM-анализ -> текст справки."""
    import api_client

    parsed = parse_repo_url(url)
    if not parsed:
        raise ValueError("Не удалось распознать ссылку на репозиторий GitHub")

    owner, repo = parsed
    meta = collect_metadata(owner, repo, token=token)
    if not meta.get("top_level") and not meta.get("manifests") and not meta.get("readme"):
        detail = "; ".join(meta.get("errors") or []) or "пустой ответ"
        raise RuntimeError(f"Не удалось собрать данные репозитория: {detail}")

    prompt = build_analysis_prompt(meta)
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    reply = api_client.call_chat_api(
        base_url, api_key, model, messages, temperature, timeout,
    )
    if isinstance(reply, dict):
        reply = reply.get("content") or reply.get("reasoning_content") or ""
    return str(reply or "").strip()