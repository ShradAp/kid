"""API chat turn: history, automatic profile memory, streaming and tool rounds.

Codex is dispatched before entering this module's turn; provider routing is unchanged.
"""

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime

import api_client
import attach_tools
import tool_gating
from app_paths import relocated_user_file
from config_manager import load_studio_history, save_studio_history
from application import chat_tools
from application.chat_messages import _prepare_messages_for_api, _strip_reasoning_from_reply
from application.session_history import _meta_title_from_prompt
from application.studio_profiles import STUDIO_ACTIVE_KEY, STUDIO_SESSION_ID, _is_studio_profile
from services.document_studio import STUDIO_HINT as _STUDIO_HINT, WORD_TOOL_NAMES as _WORD_TOOL_NAMES


class _GenerationStopped(Exception):
    """Внутренний сигнал: пользователь нажал «стоп» во время генерации /api/chat."""


def _call_chat_cancellable(cancel_event, timeout, **kwargs):
    """api_client.call_chat_api с отменой по событию.

    Прервать requests посреди чтения ответа нельзя, поэтому сам HTTP-запрос
    крутится в потоке-демоне, а вызывающий поток ожидает результат опросом
    события отмены. При «стопе» ожидание прекращается мгновенно, поток запроса
    бросается (daemon) — его ответ будет отброшен. Возврат None = отменено.
    В стриминговом режиме (stream=True) cancel_event дополнительно передаётся в
    api_client, чтобы чтение SSE-чанков прерывалось между порциями.
    """
    box: dict = {}

    def _worker():
        try:
            box["result"] = api_client.call_chat_api(
                timeout=timeout, cancel_event=cancel_event, **kwargs
            )
        except BaseException as exc:  # noqa: BLE001 — пробрасываем в вызывающий поток
            box["error"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(timeout=0.2)
        if cancel_event is not None and cancel_event.is_set():
            return None
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_turn(request, req_data, session_id, *, sessions, studio_sessions, progress,
             file_access, documents, notes, load_config, save_config, get_balance,
             log_event, memory, memory_import_error, user_data_dir, call_chat):
    studio_mode = bool(req_data.get("studio"))
    studio_persona_id = str(req_data.get("studio_persona_id") or "").strip()
    # Живой прогресс регистрируется сразу (фронтенд увидит active=true
    # с первых секунд запроса); аварийная очистка — в обработчиках
    # исключений do_POST, штатная — после отправки ответа.
    request._chat_session_id = session_id
    live_steps: list = []
    progress.thinking[session_id] = live_steps
    progress.reasoning[session_id] = ""
    # Событие отмены для POST /api/stop: выставляется кнопкой «стоп»,
    # проверяется между шагами генерации и на ожидании долгих API-вызовов.
    cancel_event = threading.Event()
    progress.cancel_events[session_id] = cancel_event

    def _cancel_requested() -> bool:
        return cancel_event.is_set()
    # Чат мог быть удалён до начала обработки запроса (устаревший клиент).
    _session_deleted = session_id in sessions.deleted
    user_prompt = req_data.get("prompt", "")
    persona_id = req_data.get("persona_id")
    attachments = req_data.get("attachments", [])
    if not isinstance(attachments, list):
        attachments = []

    found_attachments = []
    direct_data_urls = []
    attachment_notes = []

    for item in attachments:
        # Веб-клиент передаёт только ID временного файла, а не содержимое файла.
        if isinstance(item, dict):
            file_path = item.get("path")
            file_id = item.get("file_id")
            file_url = item.get("url", "")
            if file_id:
                upload_dir = os.path.join(tempfile.gettempdir(), "kid_uploads")
                matches = [name for name in os.listdir(upload_dir)] if os.path.isdir(upload_dir) else []
                matched = next((name for name in matches if name.startswith(str(file_id) + "_")), None)
                if matched:
                    found_attachments.append(os.path.abspath(os.path.join(upload_dir, matched)))
                else:
                    attachment_notes.append(f"{item.get('name', 'файл')}: временный файл не найден")
            elif file_path and file_access.allowed(relocated_user_file(file_path)):
                found_attachments.append(os.path.abspath(relocated_user_file(file_path)))
            elif file_url.startswith("data:image/") or file_url.startswith("data:video/"):
                direct_data_urls.append(file_url)
            elif file_url.startswith("data:"):
                attachment_notes.append(f"{item.get('name', 'файл')}: неподдержанный Data URL")
        # Если передана строка (путь на диске или Data URL)
        elif isinstance(item, str):
            if os.path.exists(item):
                found_attachments.append(os.path.abspath(item))
            elif item.startswith("data:image/") or item.startswith("data:video/"):
                direct_data_urls.append(item)
            elif item.startswith("data:"):
                attachment_notes.append("вложение: неподдержанный Data URL")
            else:
                attachment_notes.append(f"{item}: файл не найден или путь недоступен")

    for line in user_prompt.splitlines():
        if "Sent an attachment:" in line or "Отправлен файл:" in line:
            parts = line.split(":", 1)
            if len(parts) > 1:
                fname = parts[1].strip()
                if fname and os.path.exists(fname):
                    found_attachments.append(os.path.abspath(fname))

    cfg = load_config()
    profiles = cfg.get("profiles", {})

    if persona_id and persona_id in profiles:
        profile = profiles[persona_id]
        if cfg.get("active_profile") != persona_id:
            cfg["active_profile"] = persona_id
            save_config(cfg)
    else:
        profile = profiles.get(cfg.get("active_profile", "main"), {})

    api_base = profile.get("base_url") or profile.get(
        "api_base", "https://sprutdock.ru/v1"
    )
    api_key = profile.get("api_key", "")
    model = profile.get("model", "google/gemini-3.8-flash")
    system_prompt = profile.get("system_prompt", "")
    temperature = float(profile.get("temperature", 0.7))
    # Таймаут генерации настраивается в профиле (request_timeout);
    # connect-фаза в api_client короткая и не зависит от него.
    chat_timeout = max(10, int(profile.get("request_timeout", 120) or 120))

    # Document Studio: параметры берём из ОТДЕЛЬНОГО профиля студии.
    # Приоритет: профиль из запроса → активный профиль студии → значения
    # активного основного профиля (для незаполненных полей).
    if studio_mode:
        studio_sessions.ensure_profiles()
        cfg = load_config()
        studio_profiles = {
            name: data
            for name, data in (cfg.get("profiles") or {}).items()
            if _is_studio_profile(data)
        }
        sp_name = studio_persona_id or str(cfg.get(STUDIO_ACTIVE_KEY) or "")
        studio_profile = studio_profiles.get(sp_name) or {}
        if studio_profile:
            api_base = studio_profile.get("base_url") or api_base
            api_key = studio_profile.get("api_key") or api_key
            model = studio_profile.get("model") or model
            if str(studio_profile.get("system_prompt") or "").strip():
                system_prompt = str(studio_profile.get("system_prompt"))
            if isinstance(studio_profile.get("temperature"), (int, float)):
                temperature = float(studio_profile["temperature"])
            if (
                isinstance(studio_profile.get("request_timeout"), (int, float))
                and studio_profile["request_timeout"]
            ):
                chat_timeout = max(10, int(studio_profile["request_timeout"]))
        log_event(
            f"[STUDIO] профиль '{sp_name or '—'}': модель '{model}', "
            f"ключ {'задан' if api_key else 'не задан'}"
        )


    log_event(f"[CHAT] Сессия '{session_id}', профиль '{persona_id or cfg.get('active_profile', 'main')}', модель '{model}'")

    # --- привязка сессии к профилю + заголовок (чтобы пережить перезагрузку) ---
    # У студии своё хранилище: мета и история не смешиваются с основными чатами.
    _session_deleted = (
        session_id in studio_sessions.deleted
        if studio_mode
        else session_id in sessions.deleted
    )
    _load_meta = studio_sessions.read_metadata if studio_mode else sessions.read_metadata
    _save_meta = studio_sessions.write_metadata if studio_mode else sessions.write_metadata
    _meta_memory = studio_sessions.metadata if studio_mode else sessions.metadata
    try:
        _meta = _load_meta()
    except Exception:
        _meta = {}
    if not isinstance(_meta, dict):
        _meta = {}
    if studio_mode:
        _active_pid = studio_persona_id or str(cfg.get(STUDIO_ACTIVE_KEY) or "")
    else:
        _active_pid = persona_id or cfg.get("active_profile", "main")
    _cur = _meta.get(session_id) or {}
    _title = (req_data.get("title") or _cur.get("title") or "").strip()
    if not _title or _title in ("Новый чат", "New chat"):
        _title = _meta_title_from_prompt(user_prompt)
        # не перезаписываем осмысленный заголовок повторными сообщениями
        if _cur.get("title") and _cur.get("title") not in ("Новый чат", "New chat"):
            _title = _cur.get("title")
    if not _session_deleted:
        _meta[session_id] = {
            "personaId": _active_pid,
            "title": _title,
            "timestamp": datetime.now().strftime("%d.%m %H:%M"),
        }
        _meta_memory[session_id] = _meta[session_id]
        _save_meta(_meta)

    active_sys_prompt = (system_prompt or "").strip()
    # Дата добавляется при каждом запросе, чтобы не устаревать в долгих сессиях.
    # Это не справочная подсказка, а обязательное правило интерпретации времени.
    _today = datetime.now()
    _DATE_INSTRUCTION = (
        f"\n\n[ДАТА]\n"
        f"Текущая локальная дата и время: {_today.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(дата: {_today.strftime('%d.%m.%Y')}). "
        "Считай её единственным источником истины для слов «сегодня», «вчера», "
        "«завтра» и других относительных дат. Не называй дату из обучающих данных текущей."
    )
    active_sys_prompt += _DATE_INSTRUCTION
    # Подсказка про панель «Заметки». ВАЖНО: в системный промпт она
    # больше НЕ вклеивается безусловно. Раньше модель на каждом ходу
    # читала про save_note даже тогда, когда гейт tools не отправлял,
    # и начинала «вызывать» инструмент текстом (галлюцинация вызова).
    # Теперь подсказку добавляет tool_gating.apply_tool_prompt — и только
    # если save_note действительно есть в payload этого хода.
    _NOTES_HINT = (
        "[Панель «Заметки»: save_note(title, content) сохраняет текст, list_notes() показывает. "
        "Заметки — НЕ память профиля. Если сохранять нечего — отвечай текстом.]"
    )

    # Память профиля: ДО обращения к модели достаём из локального
    # хранилища (LanceDB + FastEmbed) факты о пользователе, извлечённые
    # из прошлых диалогов, и подмешиваем их в системный промпт. Блок
    # живёт в chat_history[0], поэтому виден и на следующих шагах
    # tool-цикла. Любой сбой памяти не должен ломать ответ.
    _memory_started = time.perf_counter()
    # Для тривиальных реплик («привет», «ок», «спасибо») факты профиля
    # бессмысленны: они раздувают промпт и провоцируют модель рассуждать,
    # применимы ли известные ей сведения к приветствию. Пропуск векторного
    # поиска экономит и время, и токены. Флаг тот же, что у гейта tools.
    _skip_memory = (
        bool(cfg.get("skip_search_on_smalltalk", True))
        and tool_gating.is_trivial(user_prompt)
    )
    try:
        _memory_block = (
            memory.build_memory_context(user_prompt)
            if (memory is not None and not _skip_memory)
            else ""
        )
    except Exception as _mem_error:
        _memory_block = ""
        log_event(f"[!] Память профиля недоступна (не критично): {_mem_error}")
    if _skip_memory:
        log_event("[MEMORY] Пропущено: тривиальная реплика, факты профиля не нужны")
    if _memory_block:
        active_sys_prompt = (
            (active_sys_prompt + _memory_block)
            if active_sys_prompt
            else _memory_block.strip()
        )
        _facts_count = _memory_block.count("\n- (")
        live_steps.append(
            f"🧠 Память профиля (локальная база, не вызов инструмента): фактов — {_facts_count}"
        )
        log_event(
            f"[MEMORY] Фактов профиля в контексте: {_facts_count} "
            f"({(time.perf_counter() - _memory_started) * 1000:.0f} мс)"
        )

    # chat_history — локальная ссылка на историю текущего запроса.
    # Если чат удалён (в т.ч. пока шёл долгий ответ ассистента), работаем
    # во временном списке и ничего не пишем на диск.
    if _session_deleted:
        chat_history: list = []
    else:
        if studio_mode:
            _history_mem = studio_sessions.history
            if session_id not in _history_mem:
                _history_mem[session_id] = load_studio_history(session_id)
        else:
            _history_mem = sessions.memory
            if session_id not in _history_mem:
                _history_mem[session_id] = sessions.load_all().get(session_id, [])
        chat_history = _history_mem[session_id]

    def _persist_history() -> None:
        """Сохраняет историю текущего чата в своё хранилище (студия/основное)."""
        try:
            if studio_mode:
                save_studio_history(session_id, chat_history)
            else:
                sessions.save_all(sessions.memory)
        except Exception as _save_error:
            log_event(f"[!] Не удалось сохранить историю чата: {_save_error}")
    if not chat_history and active_sys_prompt:
        chat_history.append({"role": "system", "content": active_sys_prompt})
    elif (
        chat_history
        and isinstance(chat_history[0], dict)
        and chat_history[0].get("role") == "system"
        and active_sys_prompt
    ):
        chat_history[0]["content"] = active_sys_prompt

    final_user_prompt = user_prompt
    images = []
    files_text = ""

    # Лимиты вложений читаются из профиля в реальном времени: перед каждым
    # build_payload кладём их в окружение, откуда attach_tools их подхватывает.
    os.environ.update({
        "ATTACH_MAX_IMAGES": str(profile.get("attach_max_images", 4)),
        "ATTACH_MAX_TEXT_CHARS": str(profile.get("attach_max_text_chars", 60000)),
        "ATTACH_MAX_TEXT_PER_FILE": str(profile.get("attach_max_text_per_file", 60000)),
        "ATTACH_MAX_TOTAL_TEXT": str(profile.get("attach_max_total_text", 180000)),
        "ATTACH_CSV_MAX_ROWS": str(profile.get("attach_csv_max_rows", 400)),
        "ATTACH_XLSX_MAX_ROWS": str(profile.get("attach_xlsx_max_rows", 400)),
        "CHAT_MSG_MAX_CHARS": str(profile.get("chat_msg_max_chars", 0)),
        "CHAT_MSG_TEXT_PART_MAX_CHARS": str(profile.get("chat_msg_text_part_max_chars", 0)),
    })

    if found_attachments:
        imgs_from_disk, files_text, build_notes = attach_tools.build_payload(found_attachments)
        images.extend(imgs_from_disk)
        attachment_notes.extend(build_notes)

    # Data URL оставлен только для обратной совместимости со старыми клиентами.
    # Новый веб-клиент всегда использует /api/upload и передаёт file_id.
    if direct_data_urls:
        images.extend(direct_data_urls)

    if attachment_notes:
        notes_text = "\n".join(f"- {note}" for note in attachment_notes)
        final_user_prompt = f"[Сведения о вложениях]\n{notes_text}\n[/Сведения о вложениях]\n\n" + final_user_prompt
    if files_text:
        final_user_prompt = files_text + "\n\n" + final_user_prompt

    # Начало текущего хода в истории. Если провайдер отклонит запрос из-за
    # фильтра контента (Content policy violation), откатим всё, что добавилось
    # за этот ход: иначе «отравленная» реплика останется в контексте и каждый
    # следующий запрос (даже короткое «1») будет снова отклонён.
    turn_start_index = len(chat_history)

    if images:
        parts = [{"type": "text", "text": final_user_prompt}]
        for du in images:
            if du.startswith("data:video/"):
                parts.append({"type": "video_url", "video_url": {"url": du}})
            else:
                parts.append({"type": "image_url", "image_url": {"url": du}})
        chat_history.append(
            {"role": "user", "content": parts}
        )
    else:
        chat_history.append(
            {"role": "user", "content": final_user_prompt}
        )

    # Сохраняем пользовательское сообщение сразу, не дожидаясь ответа API.
    # Поэтому оно не теряется при перезапуске или падении приложения.
    # ВАЖНО: content последнего сообщения НЕ перезаписываем — там промпт
    # с содержимым файлов/картинками (final_user_prompt), он уходит в API.
    # Раньше update() затирал content сырым промптом, и вложения не
    # доходили до модели в обычном режиме. Для отображения храним
    # отдельные ключи: display (сырой текст) и attachments (имена+ссылки).
    if not _session_deleted:
        _attach_meta = [
            {
                "name": str(item.get("name", "вложение")),
                "url": f"/api/download/{item.get('file_id')}" if item.get("file_id") else "",
            }
            for item in attachments
            if isinstance(item, dict) and item.get("file_id")
        ]
        _display_text = (user_prompt or "").strip()
        if not _display_text and _attach_meta:
            _display_text = "Отправлены файлы: " + ", ".join(a["name"] for a in _attach_meta)
        if chat_history and isinstance(chat_history[-1], dict):
            chat_history[-1]["display"] = _display_text
            chat_history[-1]["attachments"] = _attach_meta
        _persist_history()

    # live_steps — тот же объект, что и thinking_steps: каждый шаг
    # (поиск, чтение страницы, план архитектора) сразу виден во
    # фронтенд-опросе /api/progress.
    thinking_steps = live_steps
    # Накопленный текст «размышлений» модели за весь ход (reasoning content /
    # thought parts): сериализуется отдельным полем в сообщение ассистента
    # и возвращается фронту как reasoning (плюс живьём в /api/progress).
    reasoning_acc = ""
    final_reply = ""
    assistant_notes = []

    def _on_thinking(delta_text):
        """Льёт reasoning-токены в живой виджет Thinking process."""
        nonlocal reasoning_acc
        reasoning_acc += delta_text
        progress.reasoning[session_id] = progress.reasoning.get(session_id, "") + delta_text

    try:
        tool_loops = 0
        # Лимит шагов tool-цикла. Раньше здесь было None, поэтому
        # условие `while max_tool_loops is None or ...` не имело
        # границы, а max_steps из UI (page.tsx:753) и из профиля
        # игнорировался: модель могла звать инструменты бесконечно
        # (в истории есть ходы по 12-13 вызовов web_search).
        try:
            max_tool_loops = max(1, min(200, int(
                req_data.get("max_steps")
                or profile.get("max_steps", 60)
                or 60
            )))
        except Exception:
            max_tool_loops = 60
        # Поле max_history_messages (есть в config.json, ранее
        # использовалось только старым клиентом): ограничивает
        # размер промпта — латентность и цена на длинных чатах.
        history_cap = int(cfg.get("max_history_messages", 20) or 0) or None

        # ГЕЙТКИПЕР: решаем, какие инструменты вообще отправлять.
        # Пустой список => tools и tool_choice не попадут в payload,
        # и модель физически не сможет вызвать функцию. Это единственный
        # надёжный рычаг: поведение прокси при трансляции tool_choice
        # в нативный Gemini (function_calling_config) непроверяемо.
        # Вложения — всегда повод оставить инструменты (gate off).
        # Document Studio: вместо поиска — инструменты редактора DOCX.
        studio_hint = _STUDIO_HINT
        if studio_mode:
            # К чату может быть приложено несколько пресетов — все
            # валидируются и уходят в системный промпт для анализа.
            from services.word_automation import load_preset as _load_preset

            incoming = req_data.get("presets")
            raw_presets = (
                [p for p in incoming if isinstance(p, dict)]
                if isinstance(incoming, list)
                else []
            )
            single_preset = req_data.get("preset")
            if isinstance(single_preset, dict):
                raw_presets.append(single_preset)
            valid_presets = []
            for item in raw_presets:
                try:
                    loaded = _load_preset(item)
                    data = loaded.model_dump(mode="json")
                    data["id"] = loaded.name
                    valid_presets.append(data)
                except Exception as exc:  # noqa: BLE001 - битый пресет не рушит чат
                    log_event(f"[!] Пресет из чата студии не принят: {exc}")
            if valid_presets:
                documents.set_presets(session_id, valid_presets)
            attached = documents.state(session_id).get("attached_presets") or []
            if attached:
                names = ", ".join(
                    str(p.get("name") or p.get("id") or "?") for p in attached
                )
                studio_hint = (
                    _STUDIO_HINT
                    + f"\n\nК чату приложено пресетов: {len(attached)} ({names}). "
                    "Ниже их JSON — анализируй оформление и можешь сравнивать "
                    "пресеты между собой:\n"
                    + json.dumps(attached, ensure_ascii=False)[:8000]
                )
            turn_tools = list(api_client.WORD_TOOLS_SCHEMA)
            log_event(
                f"[STUDIO] tools в payload: {len(turn_tools)} "
                f"{tool_gating.tool_names(turn_tools)}"
            )
        else:
            _tool_mode = str(cfg.get("tool_send_mode", "gated") or "gated").strip().lower()
            if _tool_mode == "all":
                # Режим 2: всегда все тулы, модель решает сама.
                turn_tools = list(api_client.TOOLS_SCHEMA)
            elif _tool_mode == "embedding":
                # Режим 3: намерение определяет эмбеддинг-роутер.
                turn_tools = tool_gating.select_tools_by_embedding(
                    user_text=final_user_prompt,
                    all_tools=api_client.TOOLS_SCHEMA,
                    history=chat_history,
                )
            else:
                # Режим 1: маркерный гейт (как раньше).
                _gate_enabled = (
                    bool(cfg.get("skip_search_on_smalltalk", True))
                    and not (attachments or images or files_text)
                )
                turn_tools = tool_gating.select_tools(
                    user_text=final_user_prompt,
                    all_tools=api_client.TOOLS_SCHEMA,
                    history=chat_history,
                    allow_retrieval=True,
                    gate_enabled=_gate_enabled,
                )
            log_event(
                f"[GATE] tools в payload: {len(turn_tools)} "
                f"{tool_gating.tool_names(turn_tools) or '— отключены —'} | "
                f"тривиально={tool_gating.is_trivial(final_user_prompt)} "
                f"поиск={tool_gating.needs_retrieval(final_user_prompt)} "
                f"действия={list(tool_gating.needed_action_tools(final_user_prompt))} "
                f"лимит_шагов={max_tool_loops}"
            )
        if not turn_tools:
            # Формулировка важна: это НЕ вызов инструмента, а признак
            # того, что в запрос вообще не положена схема tools.
            live_steps.append(
                "🚫 Инструменты в запрос не отправлялись "
                "(реплика не требует ни поиска, ни сохранения)"
            )

        # Анти-цикл: идентичный вызов не должен повторяться
        _seen_calls = set()

        while tool_loops < max_tool_loops:
            # Пользователь нажал «стоп» — новый запрос не начинаем.
            if cancel_event.is_set():
                raise _GenerationStopped()
            safe_messages = _prepare_messages_for_api(
                chat_history, None, max_msgs=history_cap
            )
            # Промпт приводим в соответствие с фактическим payload:
            # tools нет — вырезаем упоминания инструментов, чтобы модель
            # не «вызывала» их текстом; tools есть — добавляем подсказку
            # про Заметки и «ПОРЯДОК РЕШЕНИЯ». Правится копия для API,
            # история на диске и профиль остаются нетронутыми.
            safe_messages = tool_gating.apply_tool_prompt(
                safe_messages,
                turn_tools,
                extra_hint=studio_hint if studio_mode else _NOTES_HINT,
            )

            # Отменяемый стриминговый вызов: reasoning-токены льются в
            # _on_thinking (живой виджет), при «стопе» чтение прерывается.
            resp = call_chat(
                cancel_event,
                timeout=chat_timeout,
                stream=True,
                on_thinking=_on_thinking,
                base_url=api_base,
                api_key=api_key,
                model=model,
                messages=safe_messages,
                tools=turn_tools or None,
                temperature=temperature,
            )
            if resp is None:
                raise _GenerationStopped()

            has_tool_calls = (
                isinstance(resp, dict) and bool(resp.get("tool_calls"))
            ) or (hasattr(resp, "tool_calls") and bool(getattr(resp, "tool_calls")))

            # Если модель вернула текст без вызовов инструментов — выходим, ответ готов
            if not has_tool_calls:
                break

            tool_loops += 1
            msg_obj = resp if isinstance(resp, dict) else resp.__dict__
            tool_calls = msg_obj.get("tool_calls", [])

            # Протокол: у каждого вызова обязан быть id. Если прокси
            # его не вернул, генерим уникальный ДО записи assistant —
            # иначе tool_call_id ответа не совпадёт с
            # assistant.tool_calls[].id и в истории останется
            # незакрытый вызов (модель зацикливается на нём).
            for _tc_index, _tc in enumerate(tool_calls or []):
                if isinstance(_tc, dict) and not _tc.get("id"):
                    _tc["id"] = f"call_{tool_loops}_{_tc_index}_{uuid.uuid4().hex[:8]}"

            # 1. Записываем вызов ассистента
            chat_history.append({
                "role": "assistant",
                "content": msg_obj.get("content") or "",
                "tool_calls": tool_calls
            })

            # Последний разрешённый шаг: дальше принуждаем к тексту
            is_final_step = tool_loops >= max_tool_loops

            # 2. Выполняем КАЖДЫЙ вызванный тул, чтобы не нарушить протокол API
            for tc_index, tc in enumerate(tool_calls):
                if cancel_event.is_set():
                    raise _GenerationStopped()
                tc_id = tc.get("id") or f"call_{tool_loops}_{tc_index}"
                fn_info = tc.get("function", {})
                fn_name = fn_info.get("name", "")
                raw_args = fn_info.get("arguments", "{}")

                try:
                    fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    fn_args = {}

                # АНТИ-ЦИКЛ: идентичный повтор того же инструмента с теми
                # же аргументами не тратит round-trip и не даёт модели
                # повода для нового витка (в истории были ходы по 12 вызовов).
                try:
                    _sig_args = json.dumps(
                        fn_args, ensure_ascii=False, sort_keys=True, default=str
                    )[:300]
                except Exception:
                    _sig_args = str(fn_args)[:300]
                _call_sig = f"{fn_name}::{_sig_args}"
                if _call_sig in _seen_calls:
                    log_event(
                        f"[GATE] Повторный идентичный вызов {fn_name} — цикл прерван"
                    )
                    chat_history.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": (
                            "Этот инструмент с такими же аргументами уже вызывался. "
                            "Больше не вызывай инструменты — сформируй итоговый "
                            "текстовый ответ по уже полученным данным."
                        ),
                    })
                    tool_loops = max_tool_loops
                    continue
                _seen_calls.add(_call_sig)

                tool_output = ""
                request_started = time.perf_counter()
                if fn_name == "local_search":
                    tool_output = chat_tools.local_search(fn_args, thinking_steps, request_started)
                elif fn_name == "web_search":
                    tool_output = chat_tools.web_search(fn_args, thinking_steps, request_started, profile, cancel_event, load_config, log_event)


                elif fn_name == "fetch_webpage":
                    tool_output = chat_tools.read_webpage(fn_args, thinking_steps, log_event)

                elif fn_name == "analyze_github_repo":
                    tool_output = chat_tools.analyze_repository(fn_args, thinking_steps, log_event, api_base, api_key, model, temperature)

                elif fn_name == "create_downloadable_file":
                    tool_output = chat_tools.create_file(fn_name, fn_args, thinking_steps, cancel_event)

                elif fn_name == "save_note":
                    tool_output = chat_tools.save_note(fn_args, thinking_steps, assistant_notes, notes.prepend, log_event)

                elif fn_name == "list_notes":
                    tool_output = chat_tools.list_notes(thinking_steps, notes.load)

                elif fn_name == "remember_user_fact":
                    tool_output = chat_tools.remember_user_fact(
                        fn_args, thinking_steps, chat_history, user_prompt,
                        memory, memory_import_error, log_event,
                    )
                elif fn_name == "set_system_prompt":
                    tool_output = chat_tools.set_system_prompt(fn_args, thinking_steps, profile, cfg, save_config, log_event)
                elif fn_name == "manage_memory":
                    tool_output = chat_tools.manage_memory(
                        fn_args, thinking_steps, chat_history, user_prompt,
                        memory, memory_import_error, log_event,
                    )
                elif fn_name in _WORD_TOOL_NAMES:
                    thinking_steps.append(f"📄 Правка документа студии: {fn_name}")
                    # Рабочий документ студии общий для всех её чатов —
                    # инструменты всегда адресуют один и тот же ключ.
                    tool_output = documents.run_tool(fn_name, fn_args, STUDIO_SESSION_ID)
                else:
                    tool_output = f"Инструмент '{fn_name}' не поддерживается."

                # ЖЕСТКАЯ ЗАЩИТА: режем простыни до 4000 символов (спасает баланс от HTTP 402)
                if len(str(tool_output)) > 4000:
                    tool_output = str(tool_output)[:4000] + "\n...(содержимое обрезано для экономии токенов)..."

                # Если лимит шагов исчерпан — принуждаем модель сформировать текст
                if is_final_step:
                    tool_output += "\n\n[СИСТЕМА: Это последний шаг. Больше не вызывай инструменты, сформируй подробный текстовый ответ пользователю.]"

                chat_history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": str(tool_output)
                })

        final_reply = ""

        if isinstance(resp, dict):
            final_reply = resp.get("content") or resp.get("reasoning_content") or ""
        else:
            final_reply = str(resp)

        # Модели с thinking (gemini-3.8-flash и т.п.) иногда выдают
        # размышления и в content, и в reasoning_content. Видимый ответ
        # не должен повторять блок «Thinking process» — вырезаем дубль,
        # а пустой остаток уйдёт в принудительный запрос текста ниже.
        final_reply = _strip_reasoning_from_reply(final_reply, reasoning_acc)

        # ЗАЩИТА: после последнего вызова инструмента модель иногда не успевает
        # прислать отдельное текстовое сообщение (особенно при достижении лимита
        # tool_loops), поэтому повторяем запрос без инструментов по актуальной истории.
        if not final_reply.strip():
            log_event("[*] Модель не сформировала текстовый ответ. Принудительный запрос текста...")
            fallback_messages = _prepare_messages_for_api(
                chat_history, None, max_msgs=history_cap
            )
            # Принудительный текстовый ответ идёт без tools — значит и
            # упоминаний инструментов в промпте быть не должно.
            fallback_messages = tool_gating.apply_tool_prompt(
                fallback_messages, None
            )
            if reasoning_acc.strip():
                fallback_messages.append(
                    {"role": "assistant", "content": "", "reasoning": reasoning_acc}
                )
            fallback_messages.append({
                "role": "user",
                "content": (
                    "Сформируй итоговый развернутый ответ на русском языке по сути вопроса. "
                    "Используй результаты выполненных инструментов выше, не вызывай инструменты "
                    "и не описывай процесс рассуждений. Если данных недостаточно, честно укажи это."
                ),
            })
            retry_resp = call_chat(
                cancel_event,
                timeout=chat_timeout,
                stream=True,
                on_thinking=_on_thinking,
                base_url=api_base,
                api_key=api_key,
                model=model,
                messages=fallback_messages,
                tools=None,
                temperature=temperature,
            )
            if retry_resp is None:
                raise _GenerationStopped()
            if isinstance(retry_resp, dict):
                final_reply = (
                    retry_resp.get("content")
                    or retry_resp.get("reasoning_content")
                    or retry_resp.get("thought")
                    or ""
                )
            else:
                final_reply = str(retry_resp or "")

        # Размышления модели уходят отдельным полем reasoning (не в шаги).
        if reasoning_acc:
            progress.reasoning[session_id] = reasoning_acc

        if "<" + "think>" in final_reply and "</" + "think>" in final_reply:
            parts = final_reply.split("</" + "think>", 1)
            inside_think = parts[0].replace("<" + "think>", "").strip()
            if inside_think:
                thinking_steps.extend(
                    [s.strip() for s in inside_think.split("\n") if s.strip()]
                )
            final_reply = parts[1].strip()

        if cancel_event.is_set():
            # Остановка в режиме ассистента: оборванный ответ не пишем в
            # историю — так же, как в обычном режиме (фрагмент ответа
            # не должен «воскреснуть» в контексте следующих сообщений).
            log_event(f"[STOP] Генерация прервана пользователем (сессия '{session_id}')")
        else:
            assistant_message = {"role": "assistant", "content": final_reply}
            if reasoning_acc:
                assistant_message["reasoning"] = reasoning_acc
            chat_history.append(assistant_message)
            if not _session_deleted:
                _persist_history()
            log_event(f"[CHAT] Ответ сформирован ({len(final_reply)} символов), шагов рассуждения: {len(thinking_steps)}")

    except _GenerationStopped:
        # Кнопка «стоп»: ответ не формируем и в историю не пишем —
        # оборванный ответ не должен «воскреснуть» в контексте чата.
        final_reply = "⏹ Генерация остановлена пользователем."
        thinking_steps = [str(s) for s in (thinking_steps or [])]
        log_event(f"[STOP] Генерация прервана пользователем (сессия '{session_id}')")
    except api_client.ToolCallWithoutToolsError:
        # Модель в no-tools запросе всё равно вернула tool_call (типично для
        # прокси-трансляции Gemini). Не роняем сырую ошибку API — объясняем
        # человеку, что пошло не так.
        final_reply = (
            "Не удалось сформировать текстовый ответ: модель попыталась вызвать "
            "инструмент, хотя в этом запросе инструменты не передавались. "
            "Попробуйте переформулировать запрос или повторить."
        )
        thinking_steps = [str(s) for s in (thinking_steps or [])]
        log_event("[!] Модель вернула tool_call в no-tools запросе")
    except Exception as e:
        final_reply = f"Ошибка обращения к API: {e!s}"
        log_event(f"[!] Ошибка обращения к API: {e!s}")
        # Провайдер отклонил запрос из-за фильтра контента (Content policy
        # violation и т.п.). Фильтр находится на стороне API, а не в приложении,
        # поэтому его нельзя «выключить» отсюда. Но можно не дать чату застрять:
        # последний ход (вопрос пользователя и всё, что добавилось за него)
        # убирается из истории, чтобы следующий запрос не пересылал запрещённое
        # содержимое и не получал тот же отказ снова и снова.
        if api_client.is_content_policy_error(e):
            # Провайдер не сообщает, какая именно фраза/слово сработала, поэтому
            # сохраняем локальный снимок полного контекста ДО отката. По файлу
            # можно «уполовиниванием» найти точный триггер.
            try:
                _snapshot = {
                    "session_id": session_id,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "messages": chat_history if isinstance(chat_history, list) else [],
                }
                _dbg_dir = os.path.join(
                    str(user_data_dir),
                    "history", "_content_policy_debug",
                )
                os.makedirs(_dbg_dir, exist_ok=True)
                _safe_sid = "".join(
                    ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(session_id)
                )[:80] or "session"
                _dbg_file = os.path.join(_dbg_dir, f"{_safe_sid}_{int(time.time())}.json")
                with open(_dbg_file, "w", encoding="utf-8") as _dbg_f:
                    json.dump(_snapshot, _dbg_f, ensure_ascii=False, indent=2)
                log_event(f"[CONTENT-POLICY] Снимок контекста для разбора: {_dbg_file}")
            except Exception as _dbg_err:
                log_event(f"[!] Не удалось сохранить снимок контекста: {_dbg_err}")
            _removed = 0
            try:
                _start = locals().get("turn_start_index")
                if isinstance(chat_history, list) and isinstance(_start, int) and _start >= 0:
                    while len(chat_history) > _start:
                        chat_history.pop()
                        _removed += 1
                    if not _session_deleted:
                        _persist_history()
            except Exception as _rb:
                log_event(f"[!] Не удалось откатить ход после content-policy: {_rb}")
            final_reply = (
                "⚠️ Провайдер отклонил запрос из-за политики контента "
                "(Content policy violation).\n\n"
                "Это ограничение на стороне API-провайдера, а не приложения, "
                "и оно срабатывает на содержимое конкретного сообщения. "
                "Вызвавшее отказ сообщение удалено из истории чата — можно "
                "просто продолжить беседу с новой реплики.\n\n"
                "Если отказ повторяется на нейтральных вопросах — смените модель "
                "или провайдера в настройках профиля."
            )
            log_event(
                f"[CONTENT-POLICY] Ход откачен из истории ({_removed} сообщ.), "
                f"сессия '{session_id}'"
            )

    # --- Гарантированная доставка ответа фронтенду ---
    # Любой сбой ниже раньше обрывал соединение без тела ответа,
    # и финальный ответ ассистента молча терялся на стороне интерфейса.
    try:
        if not isinstance(final_reply, str) or not final_reply.strip():
            final_reply = (
                "Агент завершил работу, но текстовый ответ не был сформирован. "
                "Подробности — в панели логов."
            )
        thinking_steps = [str(s) for s in (thinking_steps or [])]
        new_balance = get_balance(api_key, api_base)
        current_chat_chars = sum(
            len(str(m.get("content", "")))
            for m in chat_history
            if isinstance(m, dict)
        )
        response_payload = {
            "reply": final_reply,
            "thinking": thinking_steps,
            "reasoning": reasoning_acc or "",
            "balance": new_balance,
            "used_tokens": current_chat_chars // 4,
            "cancelled": cancel_event.is_set(),
        }
        if assistant_notes:
            response_payload["notes"] = assistant_notes
            # для совместимости со старым фронтом — первая заметка отдельно
            response_payload["note"] = assistant_notes[0]
    except Exception as payload_error:
        log_event(f"[!] Ошибка сборки ответа /api/chat: {payload_error}")
        response_payload = {
            "reply": final_reply
            if isinstance(final_reply, str) and final_reply.strip()
            else f"Ошибка обращения к API: {payload_error}",
            "thinking": thinking_steps if isinstance(thinking_steps, list) else [],
            "reasoning": reasoning_acc if isinstance(reasoning_acc, str) else "",
            "balance": 0,
            "used_tokens": 0,
        }

    request._set_cors()
    try:
        body_bytes = json.dumps(response_payload, ensure_ascii=False).encode(
            "utf-8", errors="replace"
        )
    except Exception as dump_error:
        log_event(f"[!] Ошибка сериализации ответа /api/chat: {dump_error}")
        body_bytes = json.dumps(
            {
                "reply": final_reply if isinstance(final_reply, str) else str(final_reply),
                "thinking": [],
                "balance": 0,
                "used_tokens": 0,
            },
            ensure_ascii=False,
        ).encode("utf-8", errors="replace")
    request.wfile.write(body_bytes)
    # Ответ отправлен — живой прогресс гасим: финальный список шагов
    # уже уехал в теле ответа (thinking[]), постоянная плашка — в сообщении.
    progress.thinking.pop(session_id, None)
    progress.reasoning.pop(session_id, None)
    progress.cancel_events.pop(session_id, None)
