"""Reuse kid's tools on Windows; all model evaluation stays on App Server."""
import json
from tool_execution import execute_tool, ToolExecutionError


def execute(name, arguments, cancel, model_call):
    def transcribe(url, quality="accurate", language=None, vocabulary="", refresh=False):
        from services.media_transcription import transcribe_media
        return transcribe_media(url, cancel, quality=quality, language=language, vocabulary=vocabulary, refresh=refresh)

    def read_page(url):
        if cancel.is_set():
            raise ToolExecutionError("Чтение отменено")
        from guest_transport import GuestSession, GuestTransportError
        import trafilatura
        fallback = False
        try:
            with GuestSession(timeout=20, max_requests=8, max_bytes=4_000_000) as session:
                response = session.get(url, timeout=(5, 15))
                if response.status_code >= 400:
                    failure = {"status": "unavailable", "reason": f"HTTP {response.status_code}",
                               "route": response.kid_route, "transport": session.diagnostics()}
                    fallback = response.status_code in (403, 408, 451, 500, 502, 503, 504)
                    if not fallback:
                        raise ToolExecutionError(json.dumps(failure, ensure_ascii=False))
                    text = ""
                else:
                    text = trafilatura.extract(response.text, include_comments=True) or ""
                    from guest_browser import unreadable_page
                    refusal = unreadable_page(text)
                    failure = {"status": "unavailable" if refusal else "empty", "reason": refusal or "no_readable_text"}
                    fallback = bool(refusal) or not text.strip()
                transport = session.diagnostics()
        except GuestTransportError as error:
            failure = {"status": "unavailable", "reason": str(error), "transport": session.diagnostics()}
            fallback = str(error) in ("network_unavailable", "regional_block", "browser_challenge")
            if not fallback:
                raise ToolExecutionError(json.dumps(failure, ensure_ascii=False)) from None
        if fallback:
            from guest_browser import read_page as render_page
            rendered = render_page(url, cancel, proxy_url=session.proxy_url)
            rendered["transport"] = session.diagnostics()
            rendered["http_reason"] = failure["reason"]
            if rendered["status"] == "read":
                return rendered
            if failure["status"] == "empty":
                return {**rendered, "status": "empty", "text_content": "",
                    "limitation": "No readable text after browser rendering; login or CAPTCHA may be required"}
            raise ToolExecutionError(json.dumps(rendered, ensure_ascii=False))
        return {"url": response.url, "text_content": text[:100000], "status": "read" if text.strip() else "empty", "content_kind": "page_text",
                "limitation": "" if text.strip() else "No readable text; JavaScript or authentication may be required",
                "route": response.kid_route, "transport": transport,
                "read_method": "text", "visual_opened": False, "source_truncated": len(text) > 100000}

    def search(query, objective=None, sources=None):
        import search_tools
        import public_sources
        from config_manager import load_config
        from research_controller import make_controller
        cfg = load_config()
        planner = make_controller({"engine": "codex"}, public_sources.SEARCH_DOMAINS,
            mode=cfg.get("search_research_mode", "adaptive"), objective=objective or query, model_call=model_call)
        results, error = search_tools.web_search(query, max_results=5, reader=read_page,
            cancel_event=cancel, planner=planner, sources=sources)
        return search_tools.format_search_block(results) if results else str(error or "Источники не найдены")

    def github(url):
        import github_analyzer as analyzer
        from guest_transport import GuestSession
        parsed = analyzer.parse_repo_url(url)
        if not parsed:
            raise ToolExecutionError("Некорректная ссылка GitHub")
        data = analyzer.collect_metadata(*parsed, token="", session_factory=GuestSession)
        if cancel.is_set():
            raise ToolExecutionError("Анализ отменён")
        return model_call([{"role": "system", "content": analyzer.ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": analyzer.build_analysis_prompt(data)}], 30)

    def local_search(query):
        import retrieval_quality
        return retrieval_quality.format_local_results(retrieval_quality.local_search(query))

    def create_file(**kwargs):
        import file_tools
        return file_tools.create_downloadable_file(**kwargs)

    def memory(handler_name, **kwargs):
        from memory import user_profile
        return getattr(user_profile, handler_name)(**kwargs)

    return execute_tool(name, arguments, {"transcribe_media": transcribe, "local_search": local_search, "web_search": search,
        "fetch_webpage": lambda url: json.dumps(read_page(url), ensure_ascii=False),
        "analyze_github_repo": github, "create_downloadable_file": create_file,
        "remember_user_fact": lambda **kwargs: memory("remember_user_fact", **kwargs),
        "manage_memory": lambda **kwargs: memory("manage_memory", **kwargs)}, cancel)
