"""API chat tool handlers with explicit storage, memory and provider dependencies."""

import time
import uuid
from datetime import datetime

import file_tools
import retrieval_quality
import search_tools
from tool_execution import execute_tool


def local_search(fn_args, thinking_steps, request_started):
    query = str(fn_args.get("query", "")).strip()
    thinking_steps.append(f"📁 Поиск в локальных документах: «{query}»")
    try:
        local_results = retrieval_quality.local_search(query)
        tool_output = retrieval_quality.format_local_results(local_results)
        retrieval_quality.record_request(bool(local_results), (time.perf_counter() - request_started) * 1000, citations=len(local_results))
    except Exception as err:
        tool_output = f"Ошибка локального поиска: {err}"
        retrieval_quality.record_request(False, (time.perf_counter() - request_started) * 1000)
    return tool_output


def web_search(fn_args, thinking_steps, request_started, profile, cancel_event, load_config, log_event):
    query = str(fn_args.get("query", "")).strip()
    thinking_steps.append(f"🔍 Поиск в сети: «{query}»")
    log_event(f"[*] Выполняется веб-поиск: {query}")
    try:
        # web_search ВСЕГДА возвращает кортеж
        # (results, error) — распаковываем, как в
        # legacy_gui/chat_app.py:1313. Раньше здесь
        # проверялся isinstance(..., list), поэтому
        # format_search_block не вызывался никогда,
        # а модель получала repr кортежа, принимала
        # его за сбой инструмента и повторяла вызов.
        search_results, search_error = search_tools.web_search(
            query,
            max_results=int(profile.get("search_results", 5) or 5),
            region=str(profile.get("search_region", "ru-ru") or "ru-ru"),
            cancel_event=cancel_event,
            progress=thinking_steps.append,
            planner=search_tools.make_research_planner(profile,
                mode=load_config().get("search_research_mode", "adaptive"),
                objective=fn_args.get("objective") or query),
            sources=fn_args.get("sources"),
        )
        if search_results:
            tool_output = (
                search_tools.format_search_block(search_results)
                or "Источники найдены, но без содержимого страниц."
            )
        elif search_error:
            tool_output = (
                f"Поиск не выполнен: {search_error}. "
                "Не повторяй этот вызов — отвечай по имеющимся данным."
            )
        else:
            tool_output = (
                "По данному запросу ничего не найдено. "
                "Не повторяй вызов — честно скажи, что данных нет."
            )
        retrieval_quality.record_request(
            bool(search_results),
            (time.perf_counter() - request_started) * 1000,
            citations=len(search_results) if isinstance(search_results, list) else 0,
        )
    except Exception as err:
        tool_output = f"Ошибка поиска: {err}. Не повторяй этот вызов."
        retrieval_quality.record_request(False, (time.perf_counter() - request_started) * 1000)
    return tool_output


def read_webpage(fn_args, thinking_steps, log_event):
    url = fn_args.get("url", "")
    thinking_steps.append(f"🌐 Чтение страницы: {url}")
    log_event(f"[*] Чтение страницы: {url}")
    try:
        # Ленивый импорт: network_agent тянет playwright
        # (тяжёлые библиотеки), который не нужен до
        # первого обращения к веб-инструментам.
        from network_agent.core import agent_core

        if getattr(agent_core, "log_cb", None) is None:
            # Решения по прокси (системный/прямой) — в логи интерфейса
            agent_core.log_cb = lambda msg: log_event(f"[NET] {msg}")
            from network_agent.proxy_env import redact_proxy_url
            state = redact_proxy_url(agent_core.proxy_url) if agent_core.proxy_url else "не найден — прямой доступ"
            agent_core.log_cb(f"🌐 Прокси: {state}")
        tool_output = agent_core.fetch_webpage(url)
    except Exception as err:
        tool_output = f"Ошибка чтения страницы: {err}"
    return tool_output


def analyze_repository(fn_args, thinking_steps, log_event, api_base, api_key, model, temperature):
    repo_url = str(fn_args.get("url", "") or "").strip()
    thinking_steps.append(f"📦 Анализ репозитория: {repo_url}")
    log_event(f"[*] Анализ репозитория GitHub: {repo_url}")
    try:
        import github_analyzer

        tool_output = github_analyzer.analyze_repository(
            repo_url,
            base_url=api_base,
            api_key=api_key,
            model=model,
            temperature=temperature,
        )
    except Exception as err:
        tool_output = f"Ошибка анализа репозитория: {err}"
    return tool_output


def create_file(fn_name, fn_args, thinking_steps, cancel_event):
    thinking_steps.append(f"📄 Создание файла: {fn_args.get('file_name', '')}")
    try:
        tool_output = execute_tool(fn_name, fn_args, {fn_name: file_tools.create_downloadable_file}, cancel_event)
    except Exception as err:
        tool_output = f"Ошибка создания файла: {err}"
    return tool_output


def save_note(fn_args, thinking_steps, assistant_notes, prepend_note, log_event):
    _nt = str(fn_args.get("title", "") or "").strip() or "Заметка от ассистента"
    _nc = str(fn_args.get("content", "") or "").strip()
    if not _nc:
        tool_output = "Ошибка: пустой content заметки, нечего сохранять."
    else:
        if len(_nt) > 120:
            _nt = _nt[:120]
        if len(_nc) > 4000:
            _nc = _nc[:4000] + "\n...(обрезано)..."
        note_id = f"note_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        note_data = {
            "id": note_id,
            "title": _nt,
            "content": _nc,
            "createdAt": datetime.now().strftime("%d.%m.%Y, %H:%M:%S"),
            "author": "assistant",
        }
        prepend_note(note_data)
        assistant_notes.append({"title": _nt, "content": _nc})
        thinking_steps.append(f"📝 Заметка сохранена: «{_nt}»")
        log_event(f"[*] Модель сохранила заметку: {_nt}")
        tool_output = f"Заметка «{_nt}» сохранена в панель «Заметки» и уже видна пользователю."
    return tool_output


def list_notes(thinking_steps, load_notes):
    thinking_steps.append("📝 Просмотр заметок")
    try:
        _all_notes = load_notes()
        if not _all_notes:
            tool_output = "В панели «Заметки» пока нет ни одной заметки."
        else:
            _lines = []
            for _i, _note in enumerate(_all_notes, 1):
                _nt2 = str(_note.get("title") or "").strip() or "Без названия"
                _nc2 = str(_note.get("content") or "").strip()
                _lines.append(f"{_i}. {_nt2}: {_nc2}")
            tool_output = "Заметки из панели «Заметки»:\n" + "\n".join(_lines)
    except Exception as _err:
        tool_output = f"Не удалось прочитать заметки: {_err}"
    return tool_output


def set_system_prompt(fn_args, thinking_steps, profile, cfg, save_config, log_event):
    _new_prompt = str(fn_args.get("prompt", "") or "").strip()
    if not _new_prompt:
        tool_output = "Ошибка: пустой системный промпт, нечего сохранять."
    else:
        if len(_new_prompt) > 8000:
            _new_prompt = _new_prompt[:8000] + "\n...(обрезано)..."
        profile["system_prompt"] = _new_prompt
        save_config(cfg)
        thinking_steps.append("⚙️ Системный промпт обновлён")
        log_event(f"[*] Модель обновила системный промпт профиля ({len(_new_prompt)} симв.)")
        tool_output = (
            "Системный промпт профиля обновлён. Новый промпт будет применяться "
            "к следующим сообщениям. Не вызывай этот инструмент повторно без новой просьбы."
        )
    return tool_output




def remember_user_fact(fn_args, thinking_steps, chat_history, user_prompt, memory, memory_import_error, log_event):
    # Долговременная память профиля: факт уходит в
    # локальный LanceDB и всплывает в следующих диалогах.
    _fact = str(fn_args.get("fact", "") or "").strip()
    _fact_cat = str(fn_args.get("category", "") or "").strip()
    thinking_steps.append(f"🧠 Запоминаю факт о пользователе: {_fact[:70]}")
    if memory is None:
        tool_output = (
            "Память профиля недоступна в этом окружении — факт не сохранён "
            f"({memory_import_error or 'модуль memory не импортирован'}). "
            "Продолжай отвечать как обычно и не повторяй вызов."
        )
        log_event(
            "[!] remember_user_fact отклонён: "
            f"{memory_import_error or 'модуль памяти не импортирован'}"
        )
    elif not _fact:
        tool_output = "Ошибка: пустой fact, нечего сохранять в память профиля."
    else:
        try:
            _mem_result = memory.remember_user_fact(_fact, _fact_cat)
            tool_output = str(_mem_result.get("message", ""))
            log_event(
                f"[MEMORY] remember_user_fact → {_mem_result.get('status')}: "
                f"[{_mem_result.get('category')}] {_fact[:80]}"
            )
            if not _mem_result.get("ok"):
                # Причина отказа (например, нет lancedb) обязана
                # попасть в лог, а не остаться внутри ответа модели.
                log_event(f"[!] Память профиля: {tool_output}")
            # Сразу освежаем блок памяти в системном промпте,
            # чтобы следующий шаг tool-цикла видел новый факт.
            if (
                _mem_result.get("ok")
                and chat_history
                and isinstance(chat_history[0], dict)
                and chat_history[0].get("role") == "system"
            ):
                _base_sys = memory.strip_memory_block(
                    chat_history[0].get("content", "")
                )
                _fresh_block = memory.build_memory_context(
                    user_prompt
                )
                chat_history[0]["content"] = (
                    (_base_sys + _fresh_block) if _fresh_block else _base_sys
                )
        except Exception as _mem_exc:
            tool_output = f"Не удалось сохранить факт в память профиля: {_mem_exc}"
    return tool_output


def manage_memory(fn_args, thinking_steps, chat_history, user_prompt, memory, memory_import_error, log_event):
    # Модель меняет долговременную память профиля по явному
    # запросу пользователя: list/update/delete/clear.
    _mem_action = str(fn_args.get("action", "") or "").strip().lower()
    _mem_id = str(fn_args.get("fact_id", "") or "").strip()
    _mem_fact = str(fn_args.get("fact", "") or "").strip()
    _mem_cat = str(fn_args.get("category", "") or "").strip()
    thinking_steps.append(f"🧠 Управление памятью профиля: {_mem_action}")
    if memory is None:
        tool_output = (
            "Память профиля недоступна в этом окружении — операция не выполнена "
            f"({memory_import_error or 'модуль memory не импортирован'}). "
            "Продолжай отвечать как обычно и не повторяй вызов."
        )
        log_event(
            "[!] manage_memory отклонён: "
            f"{memory_import_error or 'модуль памяти не импортирован'}"
        )
    else:
        try:
            _mem_result = memory.manage_memory(
                _mem_action,
                fact_id=_mem_id,
                fact=_mem_fact,
                category=_mem_cat,
            )
            tool_output = str(_mem_result.get("message", ""))
            log_event(
                f"[MEMORY] manage_memory[{_mem_action}] → "
                f"{_mem_result.get('status')}"
                + (f": {_mem_id}" if _mem_id else "")
            )
            if not _mem_result.get("ok"):
                log_event(f"[!] Память профиля: {tool_output}")
            # Освежаем блок памяти в системном промпте, чтобы
            # следующие шаги tool-цикла видели изменения сразу.
            if (
                chat_history
                and isinstance(chat_history[0], dict)
                and chat_history[0].get("role") == "system"
            ):
                _base_sys = memory.strip_memory_block(
                    chat_history[0].get("content", "")
                )
                _fresh_block = memory.build_memory_context(
                    user_prompt
                )
                chat_history[0]["content"] = (
                    (_base_sys + _fresh_block) if _fresh_block else _base_sys
                )
        except Exception as _mem_exc:
            tool_output = f"Не удалось выполнить операцию с памятью профиля: {_mem_exc}"
    return tool_output
