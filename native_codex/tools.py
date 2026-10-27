"""Reuse kid's tools on Windows; all model evaluation stays on App Server."""
import json
from tool_execution import execute_tool, ToolExecutionError


def execute(name, arguments, cancel, model_call):
    def read_page(url):
        if cancel.is_set():
            raise ToolExecutionError("Чтение отменено")
        from guest_transport import GuestSession
        import trafilatura
        with GuestSession(timeout=20, max_requests=5, max_bytes=4_000_000) as session:
            response = session.get(url, timeout=(5, 15))
            response.raise_for_status()
            text = trafilatura.extract(response.text, include_comments=True) or ""
        return {"url": response.url, "text_content": text[:100000], "status": "read",
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

    def memory(action, **kwargs):
        from memory import user_profile
        return getattr(user_profile, action)(**kwargs)

    return execute_tool(name, arguments, {"local_search": local_search, "web_search": search,
        "fetch_webpage": lambda url: json.dumps(read_page(url), ensure_ascii=False),
        "analyze_github_repo": github, "create_downloadable_file": create_file,
        "remember_user_fact": lambda **kwargs: memory("remember_user_fact", **kwargs),
        "manage_memory": lambda **kwargs: memory("manage_memory", **kwargs)}, cancel)
