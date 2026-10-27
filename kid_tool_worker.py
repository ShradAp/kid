"""Execute existing kid tools inside the isolated, proxy-only tool container."""
import contextlib
from html.parser import HTMLParser
import http.client
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, "/opt/kid/kidlib")
if Path("/opt/kid/deps").is_dir():
    sys.path.insert(0, "/opt/kid/deps")
os.environ["KID_EMBED_CACHE"] = "/embed_cache"


def main(request):
    import requests
    from tool_execution import execute_tool
    root = Path("/tools_data")
    root.mkdir(parents=True, exist_ok=True)
    proxy = f"http://127.0.0.1:{request['proxy_port']}"
    configuration = {"proxy_mode": "http", "proxy_url": proxy,
                     "search_api_key": str(request.get("search_api_key") or ""),
                     "search_research_mode": request.get("research_mode", "adaptive"),
                     "active_profile": "codex", "profiles": {"codex": {"engine": "codex"}}}
    (root / "config.json").write_text(json.dumps(configuration), encoding="utf-8")
    (root / "notes.json").write_text(json.dumps(request.get("notes", [])), encoding="utf-8")
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy}
    import net_guard
    cache = {}
    def safe(url):
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.port not in (None, 80, 443):
                return False
            if host == "localhost" or host.endswith((".local", ".internal")):
                return False
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                if host not in cache:
                    addresses = []
                    for kind in ("A", "AAAA"):
                        response = session.get("https://dns.google/resolve", params={"name": host, "type": kind}, timeout=6)
                        response.raise_for_status()
                        for item in response.json().get("Answer", []):
                            if item.get("type") in (1, 28):
                                addresses.append(ipaddress.ip_address(item["data"]))
                    cache[host] = addresses
                addresses = cache[host]
            return bool(addresses) and all(address.is_global and not net_guard._is_blocked_ip(address) for address in addresses)
        except Exception:
            return False
    # Existing SSRF checks remain active. Their DNS step is performed via DoH
    # through the SAME loopback/proxy channel; host/local DNS is unavailable.
    net_guard.is_safe_target = safe

    def read_page(url):
        target = url
        for redirect in range(5):
            if not safe(target):
                return {"url": url, "status": "blocked", "error": "unsafe_or_unresolved_target"}
            response = session.get(target, timeout=(6, 15), stream=True, allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(target, response.headers.get("Location", ""))
                response.close()
                continue
            if response.status_code != 200:
                response.close()
                return {"url": target, "status": "blocked", "error": "http_error"}
            raw = net_guard.read_bounded(response, net_guard.MAX_HTML_BYTES)
            if raw is None:
                return {"url": target, "status": "blocked", "error": "byte_limit"}
            html = raw.decode(response.encoding or "utf-8", errors="replace")
            if importlib.util.find_spec("trafilatura"):
                import trafilatura
                text = trafilatura.extract(html, include_comments=True) or ""
            else:
                class TextReader(HTMLParser):
                    def __init__(self):
                        super().__init__(); self.parts = []; self.skip = 0
                    def handle_starttag(self, tag, attrs):
                        if tag in ("script", "style", "nav", "header", "footer"):
                            self.skip += 1
                    def handle_endtag(self, tag):
                        if tag in ("script", "style", "nav", "header", "footer") and self.skip:
                            self.skip -= 1
                    def handle_data(self, data):
                        if not self.skip and data.strip():
                            self.parts.append(data.strip())
                parser = TextReader(); parser.feed(html); text = "\n".join(parser.parts)
            return {"url": target, "text_content": text[:100000], "title": target, "status": "read",
                    "read_method": "text", "visual_opened": False, "source_truncated": len(text) > 100000}
        return {"url": url, "status": "blocked", "error": "redirect_limit"}

    def model_call(messages, timeout):
        connection = http.client.HTTPConnection("127.0.0.1", request["relay_port"], timeout=min(45, timeout) + 5)
        try:
            body = {"session_id": request["session_id"], "messages": messages, "timeout": min(30, timeout)}
            connection.request("POST", "/model", body=json.dumps(body), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read(200000))
            if response.status != 200 or "text" not in result:
                raise RuntimeError("Контроллер Codex недоступен")
            return result["text"]
        finally:
            connection.close()

    def web_search(query, objective=None, sources=None):
        import public_sources
        import search_tools
        from research_controller import make_controller
        search_tools.load_config = lambda: configuration
        search_tools.SERPER_API_KEY = configuration["search_api_key"]
        if not search_tools.SERPER_API_KEY:
            return "Поиск kid недоступен: настройте Serper. Локальный SearXNG не подключается вне изолированного маршрута."
        planner = make_controller({"engine": "codex"}, public_sources.SEARCH_DOMAINS,
                                  mode=configuration["search_research_mode"], objective=objective or query, model_call=model_call)
        results, error = search_tools.web_search(query, backend="serper", max_results=5,
                                                 reader=read_page, sources=sources, planner=planner)
        return search_tools.format_search_block(results) if results else str(error or "Источники не найдены")

    def github(url):
        import github_analyzer
        parsed = github_analyzer.parse_repo_url(url)
        if not parsed:
            raise ValueError("Некорректная ссылка на репозиторий")
        metadata = github_analyzer.collect_metadata(*parsed, token="", session_factory=lambda: session)
        if not metadata.get("readme") and not metadata.get("top_level"):
            raise RuntimeError("Метаданные GitHub недоступны через прокси")
        return model_call([{"role": "system", "content": github_analyzer.ANALYSIS_SYSTEM_PROMPT},
                           {"role": "user", "content": github_analyzer.build_analysis_prompt(metadata)}], 30)

    def memory(name, **arguments):
        from memory import user_profile
        native = root / "data" / "lancedb"
        if native.is_dir() and any(native.iterdir()) and importlib.util.find_spec("lancedb") is None:
            return "Память недоступна: защищённому образу нужен пакет lancedb. Исходная база не изменена."
        user_profile.set_log_callback(lambda text: None)
        return getattr(user_profile, name)(**arguments)

    import file_tools
    import retrieval_quality
    handlers = {
        "local_search": lambda query: retrieval_quality.format_local_results(retrieval_quality.local_search(query)),
        "create_downloadable_file": file_tools.create_downloadable_file,
        "remember_user_fact": lambda **args: memory("remember_user_fact", **args),
        "manage_memory": lambda **args: memory("manage_memory", **args),
        "web_search": web_search,
        "fetch_webpage": lambda url: json.dumps(read_page(url), ensure_ascii=False),
        "analyze_github_repo": github,
    }
    return execute_tool(request["name"], request.get("arguments", {}), handlers)


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        # Existing libraries may print diagnostics; never put these on the
        # protocol channel or in host logs.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main(request)
        print(json.dumps({"result": result}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "Инструмент завершился ошибкой в защищённой среде"}))
