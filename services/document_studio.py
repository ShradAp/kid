"""Studio document workflow; optional Word dependencies are imported on demand."""

import json
import os
import threading
import urllib.parse

from tool_schemas import WORD_TOOLS_SCHEMA

WORD_TOOL_NAMES = {(tool.get("function") or {}).get("name")
                   for tool in WORD_TOOLS_SCHEMA if isinstance(tool, dict)}

STUDIO_HINT = (
    "[Document Studio: к студии прикреплён рабочий DOCX. Ты правишь именно его. "
    "Доступные инструменты: get_document_state (показать состояние), set_page_margins, "
    "set_paragraph_format, set_font, format_tables (выравнивание/шрифт/отступы таблиц), "
    "align_figures, reset_spacing (интервалы в 0), apply_document_preset, "
    "apply_document_rules; для анализа оформления — get_document_preset (приложенные к чату "
    "пресеты), get_preset (сохранённый пресет по имени), list_available_presets. "
    "Отдельные области: title_page (титульный лист — шапка/заголовок/подписи/подвал не "
    "наследуют абзацный отступ основного текста), special_sections (реферат/задание — "
    "название по центру, текст по ширине), page_numbering (нумерация страниц: колонтитул, "
    "старт, скрытие на титульнике/задании). "
    "Правки применяются к файлу сразу и обновляют предпросмотр по центру. "
    "Сначала при необходимости вызови get_document_state, затем действуй.]"
)



class DocumentStudio:
    def __init__(self, resolve_document, download_url, log):
        self.resolve_document = resolve_document
        self.download_url = download_url
        self.log = log
        self.documents = {}
        self.lock = threading.Lock()

    def presets(self, ) -> dict:
        """Пресеты оформления из services/word_automation/presets (для Document Studio)."""
        try:
            from services.word_automation.preset_schema import list_presets, load_preset
        except Exception as exc:  # noqa: BLE001 - модуль может быть не установлен
            return {"presets": [], "error": f"word_automation недоступен: {exc}"}
        presets: list[dict] = []
        for name in list_presets():
            try:
                preset = load_preset(name)
            except Exception as exc:  # noqa: BLE001 - битый пресет не рушит список
                self.log(f"[!] Пресет {name} не загружен: {exc}")
                continue
            data = preset.model_dump(mode="json")
            data["id"] = preset.name
            presets.append(data)
        return {"presets": presets}


    def compile(self, req_data: dict) -> dict:
        """Собирает новый DOCX из блоков либо нормализует приложенный файл."""
        from services.word_automation import DocxFormatter

        preset = str(req_data.get("preset") or "").strip() or None
        source_reference = str(req_data.get("source_file") or "").strip()
        formatter = DocxFormatter()
        if source_reference:
            source_path = self.resolve_document(source_reference)
            if not source_path:
                raise FileNotFoundError(f"Исходный документ не найден: {source_reference}")
            output_path = formatter.normalize_document(source_path, preset=preset)
        else:
            blocks = req_data.get("blocks")
            if not isinstance(blocks, list) or not blocks:
                raise ValueError("Передайте blocks или source_file")
            output_path = formatter.create_document(blocks, preset=preset)
        name = os.path.basename(output_path)
        return {
            "ok": True,
            "path": output_path,
            "filename": name,
            "stats": dict(formatter.last_stats),
            "download": "/api/download-local?path=%s&name=%s"
            % (urllib.parse.quote(output_path), urllib.parse.quote(name)),
        }


    def state(self, session_id: str) -> dict:
        """Возвращает (создавая при необходимости) состояние документа студии."""
        with self.lock:
            state = self.documents.get(session_id)
            if state is None:
                state = {
                    "path": "", "name": "", "preset": "",
                    "preset_json": None, "attached_presets": [],
                }
                self.documents[session_id] = state
            state.setdefault("attached_presets", [])
            return state


    def register(self, 
        session_id: str,
        path: str = "",
        name: str = "",
        preset_name: str = "",
        preset_json: object = None,
    ) -> None:
        """Обновляет состояние документа студии, не теряя активный пресет."""
        with self.lock:
            state = self.documents.get(session_id) or {
                "path": "", "name": "", "preset": "", "preset_json": None,
                "attached_presets": [],
            }
            if path:
                state["path"] = path
            if name:
                state["name"] = name
            if preset_name:
                state["preset"] = preset_name
            if preset_json is not None:
                state["preset_json"] = preset_json
            state.setdefault("attached_presets", [])
            self.documents[session_id] = state


    def set_preset(self, session_id: str, preset_json: object) -> None:
        """Запоминает активный пресет оформления для чата студии."""
        self.register(session_id, preset_json=preset_json)


    def set_presets(self, session_id: str, presets_json: list) -> None:
        """Запоминает список пресетов, приложенных к чату (их может быть несколько)."""
        with self.lock:
            # Лок берётся ровно один раз: STUDIO_LOCK нереентерабельный,
            # поэтому _studio_state() здесь вызывать нельзя (был бы дедлок).
            state = self.documents.get(session_id)
            if state is None:
                state = {
                    "path": "", "name": "", "preset": "",
                    "preset_json": None, "attached_presets": [],
                }
                self.documents[session_id] = state
            state["attached_presets"] = list(presets_json or [])
            if presets_json:
                state["preset_json"] = presets_json[-1]


    def reference(self, req_data: dict) -> str:
        """Ссылка на документ: явный путь/file_id либо текущий файл сессии."""
        reference = str(
            req_data.get("source_file") or req_data.get("file_id") or ""
        ).strip()
        if reference:
            return reference
        session_id = str(req_data.get("session_id") or "document-studio")
        return str(self.state(session_id).get("path") or "")


    def analyze(self, req_data: dict) -> dict:
        """Анализирует DOCX и отдаёт предпросмотр + отчёт БЕЗ создания пресета.

        Пресет больше НЕ создаётся автоматически: раньше каждый анализ писал новый
        файл в ``services/word_automation/presets``, поэтому каталог разрастался
        дубликатами вида ``*_2.json`` и в UI появлялись «фантомные» пресеты.
        Сохранение теперь явное — через ``/api/word/preset-save`` либо по флагу
        ``save_preset`` / явно заданному ``preset_name``.

        Результат анализа остаётся в состоянии сессии (``preset_json``), чтобы
        предпросмотр и модель могли им пользоваться без записи на диск.
        """
        from services.word_automation import analyze_document

        reference = self.reference(req_data)
        source_path = self.resolve_document(reference)
        if not source_path:
            raise FileNotFoundError(f"Документ не найден: {reference or 'не передан'}")
        session_id = str(req_data.get("session_id") or "document-studio")
        name = str(req_data.get("preset_name") or "").strip() or None
        result = analyze_document(source_path, name=name)

        wants_save = bool(req_data.get("save_preset")) or bool(name)
        saved = ""
        if wants_save:
            try:
                from services.word_automation import save_preset

                saved = save_preset(result.preset)
            except Exception as exc:  # noqa: BLE001 - неудачное сохранение не рушит анализ
                self.log(f"[!] Не удалось сохранить пресет '{result.preset.name}': {exc}")

        self.register(
            session_id,
            path=source_path,
            name=os.path.basename(source_path),
            # Имя пресета регистрируем только если он реально лежит на диске,
            # иначе apply_preset(preset="<имя>") не найдёт файл.
            preset_name=result.preset.name if saved else "",
        )
        if not saved:
            # _studio_register не трогает поле при пустой строке, поэтому чистим
            # вручную: имя пресета предыдущего документа к новому не относится.
            with self.lock:
                state = self.documents.get(session_id)
                if state is not None:
                    state["preset"] = ""
        preset_data = result.preset.model_dump(mode="json")
        preset_data["id"] = result.preset.name
        self.set_preset(session_id, preset_data)
        return {
            "ok": True,
            "preset": preset_data,
            "preview": result.preview,
            "report": result.report,
            "preset_path": saved,
            "preset_saved": bool(saved),
            "source_name": os.path.basename(source_path),
        }


    def save_document_preset(self, req_data: dict) -> dict:
        """Явно извлекает и сохраняет пресет оформления документа студии.

        Вызывается действием пользователя («Извлечь пресет»): принимает либо готовый
        JSON пресета из UI, либо сам анализирует документ. Это единственное место,
        где пресет записывается на диск.
        """
        from services.word_automation import save_preset

        session_id = str(req_data.get("session_id") or "document-studio")
        name = str(req_data.get("preset_name") or "").strip() or None

        preset_json = req_data.get("preset")
        if isinstance(preset_json, dict) and preset_json:
            from services.word_automation.preset_schema import StylePreset

            preset = StylePreset.model_validate(preset_json)
            if name:
                preset.name = name
        else:
            from services.word_automation import analyze_document

            reference = self.reference(req_data)
            source_path = self.resolve_document(reference)
            if not source_path:
                raise FileNotFoundError(
                    f"Документ не найден: {reference or 'не передан'}"
                )
            preset = analyze_document(source_path, name=name).preset

        saved = save_preset(preset)
        preset_data = preset.model_dump(mode="json")
        preset_data["id"] = preset.name
        self.register(session_id, preset_name=preset.name)
        self.set_preset(session_id, preset_data)
        self.log(f"[+] Пресет '{preset.name}' извлечён пользователем -> {saved}")
        return {
            "ok": True,
            "preset": preset_data,
            "preset_path": saved,
            "preset_saved": True,
            "name": preset.name,
        }


    def document_state(self, req_data: dict) -> dict:
        """Возвращает состояние студии: открытый файл, пресет и предпросмотр.

        Фронтенд вызывает его при возврате в Document Studio после перехода между
        разделами: файл и пресет живут на бэкенде, поэтому документ, анализ и
        предпросмотр восстанавливаются без повторной загрузки пользователем.
        """
        session_id = str(req_data.get("session_id") or "document-studio")
        state = self.state(session_id)
        path = str(state.get("path") or "")
        name = str(state.get("name") or "") or (os.path.basename(path) if path else "")
        payload: dict = {
            "ok": True,
            "session_id": session_id,
            "path": path,
            "name": name,
            "preset": str(state.get("preset") or ""),
            "preset_json": state.get("preset_json"),
            "attached_presets": list(state.get("attached_presets") or []),
            "has_document": bool(path and os.path.isfile(path)),
        }
        if payload["has_document"]:
            payload["download"] = self.download_url(path, name)
            try:
                from services.word_automation import preview_document

                payload["preview"] = preview_document(path)
            except Exception as exc:  # noqa: BLE001 - предпросмотр не критичен
                self.log(f"[!] Предпросмотр студии не построен: {exc}")
        return payload


    def close(self, req_data: dict) -> dict:
        """Закрывает документ студии: сбрасывает файл, пресет и анализ сессии.

        Явное действие «Закрыть документ» — без него состояние сессии жило бы до
        перезапуска моста, и следующий документ «наследовал» бы прошлый пресет.
        """
        session_id = str(req_data.get("session_id") or "document-studio")
        with self.lock:
            state = self.documents.pop(session_id, None)
        closed_name = str((state or {}).get("name") or "")
        self.log(f"[+] Документ студии закрыт: {closed_name or '(пусто)'}")
        return {
            "ok": True,
            "closed": closed_name,
            "path": "",
            "name": "",
            "preset": "",
            "preset_json": None,
            "attached_presets": [],
            "has_document": False,
        }


    def preview(self, req_data: dict) -> dict:
        """Возвращает модель предпросмотра текущего документа студии."""
        from services.word_automation import preview_document

        reference = self.reference(req_data)
        source_path = self.resolve_document(reference)
        if not source_path:
            raise FileNotFoundError(f"Документ не найден: {reference or 'не передан'}")
        return {
            "ok": True,
            "preview": preview_document(source_path),
            "source_name": os.path.basename(source_path),
        }


    def apply(self, req_data: dict) -> dict:
        """Применяет пресет и/или список правил к документу студии (in-place)."""
        from services.word_automation import apply_preset, apply_rules, preview_document

        session_id = str(req_data.get("session_id") or "document-studio")
        state = self.state(session_id)
        reference = self.reference(req_data)
        source_path = self.resolve_document(reference)
        if not source_path:
            raise FileNotFoundError(f"Документ не найден: {reference or 'не передан'}")

        preset = req_data.get("preset")
        rules = req_data.get("rules")
        stats: dict = {}
        if preset:
            outcome = apply_preset(source_path, preset)
            stats.update(outcome.get("stats") or {})
            if isinstance(preset, dict):
                state["preset"] = str(preset.get("name") or state.get("preset") or "")
                self.set_preset(session_id, preset)
            elif isinstance(preset, str):
                state["preset"] = preset
                try:
                    from services.word_automation import load_preset

                    loaded = load_preset(preset)
                    stored = loaded.model_dump(mode="json")
                    stored["id"] = loaded.name
                    self.set_preset(session_id, stored)
                except Exception:  # noqa: BLE001 - имя могло не найтись, это не критично
                    pass
        elif rules:
            outcome = apply_rules(source_path, rules, output_path=source_path)
            stats.update(outcome.get("stats") or {})
        else:
            raise ValueError("Передайте preset или rules")

        target = outcome.get("path", source_path)
        self.register(
            session_id, target, os.path.basename(target), str(state.get("preset") or "")
        )
        filename = os.path.basename(target)
        return {
            "ok": True,
            "path": target,
            "filename": filename,
            "stats": stats,
            "preview": preview_document(target),
            "download": self.download_url(target, filename),
        }


    def run_tool(self, fn_name: str, fn_args: dict, session_id: str) -> str:
        """Исполняет инструмент редактирования DOCX и возвращает текст модели.

        Все правки физически применяются к файлу студии (in-place) и сразу видны в
        центральном предпросмотре после обновления страницы.
        """
        from services.word_automation import (
            PresetError,
            apply_preset,
            apply_rules,
            preview_document,
        )

        fn_args = fn_args or {}
        state = self.state(session_id)

        # Инструменты анализа пресетов работают и без прикреплённого документа.
        if fn_name == "list_available_presets":
            from services.word_automation.preset_schema import list_presets

            names = list_presets()
            return (
                "Доступные пресеты оформления: " + ", ".join(names)
                if names
                else "Сохранённых пресетов оформления нет."
            )

        if fn_name == "get_document_preset":
            attached = state.get("attached_presets") or []
            if attached:
                return (
                    "Пресеты оформления, приложенные к чату (JSON):\n"
                    + json.dumps(attached, ensure_ascii=False)
                )
            active = state.get("preset_json")
            if not active:
                active_name = str(state.get("preset") or "").strip()
                if active_name:
                    try:
                        from services.word_automation import load_preset

                        loaded = load_preset(active_name)
                        active = loaded.model_dump(mode="json")
                        active["id"] = loaded.name
                        self.set_preset(session_id, active)
                    except Exception:  # noqa: BLE001 - пресет мог быть переименован
                        active = None
            if not active:
                return (
                    "Активный пресет не задан. Отправьте пресет из правой панели "
                    "(кнопка «Отправить пресет в модель») или прикрепите документ."
                )
            return "Активный пресет оформления (JSON):\n" + json.dumps(active, ensure_ascii=False)

        if fn_name == "get_preset":
            preset_name = str(fn_args.get("preset_name") or "").strip()
            if not preset_name:
                return "Укажите preset_name (имя сохранённого пресета)."
            from services.word_automation.preset_schema import list_presets, preset_path

            # Проверяем наличие файла заранее: load_preset мог бы пересоздать
            # удалённый базовый пресет — здесь это нежелательно.
            if not preset_path(preset_name).exists():
                available = ", ".join(list_presets()) or "нет"
                return f"Пресет '{preset_name}' не найден. Доступные пресеты: {available}."
            from services.word_automation import load_preset

            try:
                loaded = load_preset(preset_name)
            except Exception as exc:  # noqa: BLE001 - внятное сообщение модели
                return f"Пресет '{preset_name}' не найден: {exc}"
            data = loaded.model_dump(mode="json")
            data["id"] = loaded.name
            self.set_preset(session_id, data)
            return f"Пресет '{loaded.name}' (JSON):\n" + json.dumps(data, ensure_ascii=False)

        path = str(state.get("path") or "")
        if not path or not os.path.isfile(path):
            return (
                "Документ ещё не прикреплён к студии. Попросите пользователя добавить "
                ".docx в Document Studio — после этого он станет доступен инструментам."
            )

        def _summarize(prefix: str, stats: dict) -> str:
            return (
                f"{prefix} Готово. Изменено: абзацев {stats.get('paragraphs_touched', 0)}, "
                f"таблиц {stats.get('tables_touched', 0)}, "
                f"рисунков {stats.get('figures_touched', 0)}, "
                f"секций {stats.get('sections_touched', 0)}."
            )

        try:
            if fn_name == "get_document_state":
                preview = preview_document(path)
                page = preview.get("page") or {}
                tables = sum(1 for b in preview.get("blocks", []) if b.get("type") == "table")
                images = sum(1 for b in preview.get("blocks", []) if b.get("type") == "image")
                return (
                    f"Документ «{state.get('name') or os.path.basename(path)}»: поля "
                    f"{page.get('top')}/{page.get('bottom')}/{page.get('left')}/"
                    f"{page.get('right')} мм, блоков {len(preview.get('blocks', []))}, "
                    f"таблиц {tables}, рисунков {images}."
                )

            if fn_name == "apply_document_preset":
                preset_name = str(fn_args.get("preset_name") or "").strip()
                if not preset_name:
                    return "Не указано имя пресета для применения."
                outcome = apply_preset(path, preset_name)
                state["preset"] = preset_name
                try:
                    from services.word_automation import load_preset

                    loaded = load_preset(preset_name)
                    stored = loaded.model_dump(mode="json")
                    stored["id"] = loaded.name
                    self.set_preset(session_id, stored)
                except Exception:  # noqa: BLE001 - активный пресет останется прежним
                    pass
                return _summarize(
                    f"Применён пресет «{preset_name}».", outcome.get("stats") or {}
                )

            # Остальные инструменты собираются в список правил.
            rules: list[dict] = []
            if fn_name == "set_page_margins":
                for key in ("top", "bottom", "left", "right"):
                    if fn_args.get(key) is not None:
                        rules.append({"scope": "page", "property": key, "value": fn_args[key]})
            elif fn_name == "set_paragraph_format":
                scope = str(fn_args.get("scope") or "body")
                for key in (
                    "space_before", "space_after", "line_spacing",
                    "line_spacing_rule", "first_line_indent", "alignment",
                ):
                    if fn_args.get(key) is not None:
                        rules.append({"scope": scope, "property": key, "value": fn_args[key]})
            elif fn_name == "set_font":
                scope = str(fn_args.get("scope") or "body")
                for key in ("font_name", "font_size", "bold", "italic"):
                    if fn_args.get(key) is not None:
                        rules.append({"scope": scope, "property": key, "value": fn_args[key]})
            elif fn_name == "format_tables":
                mapping = {
                    "table_alignment": "table_alignment",
                    "cell_alignment": "alignment",
                    "font_name": "font_name",
                    "font_size": "font_size",
                    "cell_margin_top": "cell_margin_top",
                    "cell_margin_bottom": "cell_margin_bottom",
                    "cell_margin_left": "cell_margin_left",
                    "cell_margin_right": "cell_margin_right",
                }
                for arg_key, prop in mapping.items():
                    if fn_args.get(arg_key) is not None:
                        rules.append({"scope": "tables", "property": prop, "value": fn_args[arg_key]})
            elif fn_name == "align_figures":
                if fn_args.get("alignment") is not None:
                    rules.append(
                        {"scope": "figures", "property": "alignment", "value": fn_args["alignment"]}
                    )
            elif fn_name == "reset_spacing":
                scope = str(fn_args.get("scope") or "all")
                rules.extend(
                    [
                        {"scope": scope, "property": "space_before", "value": 0},
                        {"scope": scope, "property": "space_after", "value": 0},
                        {"scope": scope, "property": "line_spacing", "value": 1.0},
                        {"scope": scope, "property": "line_spacing_rule", "value": "single"},
                    ]
                )
            elif fn_name == "apply_document_rules":
                raw = fn_args.get("rules")
                if isinstance(raw, list):
                    rules.extend(raw)
            else:
                return f"Инструмент '{fn_name}' не поддерживается."

            if not rules:
                return "Не передано ни одного значения для изменения — уточните параметры."

            outcome = apply_rules(path, rules, output_path=path)
            return _summarize(f"Правил применено: {len(rules)}.", outcome.get("stats") or {})
        except PresetError as exc:
            return f"Не удалось применить правило: {exc}"
        except Exception as exc:  # noqa: BLE001 - инструмент не должен ронять чат
            return f"Ошибка инструмента '{fn_name}': {exc}"


    def save_preset(self, req_data: dict) -> dict:
        """Сохраняет присланный пресет в ``presets/`` под заданным именем.

        Пресет проходит валидацию схемы (:func:`validate_preset_payload`), которая
        теперь поддерживает блоки ``title_page``, ``special_sections`` и
        ``page_numbering``; предупреждения валидатора возвращаются фронтенду.
        """
        from services.word_automation import (
            load_preset,
            save_preset,
            validate_preset_payload,
        )

        data = req_data.get("preset")
        if not isinstance(data, dict):
            raise ValueError("Передайте preset (объект пресета)")
        report = validate_preset_payload(data)
        if not report.get("ok"):
            problems = "; ".join(
                err.get("message", "")
                if isinstance(err, dict)
                else str(err)
                for err in report.get("errors", [])
            )
            raise ValueError(f"Пресет не прошёл валидацию: {problems}")
        preset = load_preset(data)
        name = str(req_data.get("name") or "").strip() or None
        path = save_preset(preset, name)
        # save_preset мог переименовать пресет — читаем финальную версию с диска.
        final = load_preset(path)
        payload = final.model_dump(mode="json")
        payload["id"] = final.name
        self.log(f"[*] Пресет сохранён: {path}")
        return {
            "ok": True,
            "path": path,
            "preset": payload,
            "warnings": report.get("warnings", []),
            "sections": report.get("sections", []),
        }


