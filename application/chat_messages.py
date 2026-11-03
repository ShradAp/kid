"""Pure chat history preparation and visible reply transformations."""

import json
import os

def _collapse_ws(text):
    """Схлопывает пробелы и переносы строк для сравнения текстов."""
    return " ".join((text or "").split())


# Не вырезаем короткие «размышления»: это защита от ложных срабатываний,
# когда реальный ответ случайно начинается с тех же нескольких слов.
_MIN_REASONING_STRIP_CHARS = 60


def _strip_reasoning_from_reply(final_reply, reasoning_acc):
    """Вырезает из видимого ответа продублированные моделью размышления.

    Модели с нативным thinking (например, google/gemini-3.8-flash) иногда
    выдают цепочку рассуждений сразу в двух местах: в reasoning_content
    (уходит в блок «Thinking process») и в content (становится видимым
    ответом). Тогда ответ буквально повторяет размышления — убираем это
    повторение, оставляя только реальный текст.
    """
    if not final_reply or not reasoning_acc:
        return final_reply
    reason_norm = _collapse_ws(reasoning_acc)
    if len(reason_norm) < _MIN_REASONING_STRIP_CHARS:
        return final_reply
    reply_norm = _collapse_ws(final_reply)
    # Ответ целиком состоит из размышлений — видимого текста нет.
    if reply_norm == reason_norm:
        return ""
    # Размышления продублированы перед реальным ответом — отрезаем префикс.
    if reply_norm.startswith(reason_norm):
        stripped = (final_reply or "").strip()
        reason_stripped = (reasoning_acc or "").strip()
        if reason_stripped and stripped.startswith(reason_stripped):
            return stripped[len(reason_stripped):].strip()
        # Форматирование разошлось (переносы/пробелы) — режем по
        # нормализованному префиксу как запасной вариант.
        return reply_norm[len(reason_norm):].strip()
    return final_reply


def _api_safe_msg(m: dict) -> dict:
    """Оставляет только ключи, легальные для chat-completions API.

    В истории хранятся служебные поля (display — текст для отображения,
    attachments — чипсы файлов); они не должны утекать в JSON-запрос к модели.
    """
    out = {"role": m.get("role"), "content": m.get("content", "")}
    if m.get("tool_calls"):
        out["tool_calls"] = m["tool_calls"]
    if m.get("tool_call_id"):
        out["tool_call_id"] = m["tool_call_id"]
    return out


def _repair_tool_chain(msgs: list) -> list:
    """Чинит начало окна истории под требования OpenAI-совместимого протокола.

    Цикл (а не одиночный if): после удаления битого assistant с tool_calls
    следом могут остаться его tool-ответы — их тоже нужно снять, иначе запрос
    начинается с role="tool" (400 у строгих провайдеров, зацикливание у прокси).
    """
    changed = True
    while msgs and changed:
        changed = False
        head = msgs[0] if isinstance(msgs[0], dict) else {}
        if head.get("role") not in ("user", "assistant"):
            msgs.pop(0)
            changed = True
            continue
        if head.get("role") == "assistant" and head.get("tool_calls"):
            _need = {
                tc.get("id")
                for tc in head["tool_calls"]
                if isinstance(tc, dict)
            }
            _have = {
                m.get("tool_call_id")
                for m in msgs[1:]
                if isinstance(m, dict) and m.get("role") == "tool"
            }
            if _need and not _need.issubset(_have):
                msgs.pop(0)
                while msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "tool":
                    msgs.pop(0)
                changed = True
    return msgs


def _prepare_messages_for_api(
    messages: list, max_chars: int | None = 24000, max_msgs: int | None = None
) -> list:
    """Подготавливает историю перед отправкой в LLM.

    Для обычного режима max_chars=None отключает клиентское ограничение размера
    истории и содержимого сообщений. Ограничения контекста в этом режиме
    остаются ответственностью API-провайдера.

    max_msgs — лимит количества сообщений (system не считается): растущая
    история не должна раздувать каждый запрос к модели (латентность и цена).
    Управляется config.json → max_history_messages (0 = без лимита).

    Текущий ход — от последнего user-сообщения до конца массива — не режется:
    в цикле tool_calls он растёт (assistant+tool на каждый шаг), и оконный
    срез иначе выталкивает исходный вопрос пользователя из окна — модель
    остаётся без вопроса и отвечает базовым приветствием на голый системный
    промпт (проявлялось после ~10 циклов поиска при max_history_messages=20).
    Лимит применяется только к сообщениям ПРЕДЫДУЩИХ ходов.
    """
    if not messages:
        return []
    system_msg = None
    other_msgs = messages
    if messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        other_msgs = messages[1:]

    if max_msgs:
        # Индекс последнего user-сообщения — граница текущего хода.
        last_user_idx = None
        for i in range(len(other_msgs) - 1, -1, -1):
            if (other_msgs[i] or {}).get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            if len(other_msgs) > max_msgs:
                other_msgs = other_msgs[-max_msgs:]
        else:
            current_turn = other_msgs[last_user_idx:]
            prior = other_msgs[:last_user_idx]
            prior_budget = max(0, max_msgs - len(current_turn))
            if len(prior) > prior_budget:
                prior = prior[-prior_budget:] if prior_budget else []
            other_msgs = prior + current_turn
    # Целостность протокола — ВНЕ зависимости от лимита: раньше блок жил внутри
    # `if max_msgs:`, и при max_history_messages=0 (без лимита) валидации истории
    # не было вовсе, а на диске tool-цепочки сохраняются как есть.
    other_msgs = _repair_tool_chain(other_msgs)

    def _msg_len(m):
        try:
            return len(json.dumps(m.get("content", ""), ensure_ascii=False, default=str))
        except Exception:
            return len(str(m.get("content", "")))

    # Находим самое свежее user-сообщение с картинками — только ему оставляем images
    last_img_idx = -1
    for i in range(len(other_msgs) - 1, -1, -1):
        c = (other_msgs[i] or {}).get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            last_img_idx = i
            break

    # Лимиты на отдельное сообщение задаются в профиле (0 = без лимита) и
    # приходят через окружение. Раньше здесь были жёсткие 8000/12000 символов,
    # из-за чего содержимое вложений и сам запрос пользователя обрезались.
    def _env_lim(name):
        try:
            v = int(os.environ.get(name, "0") or "0")
            return v if v > 0 else 0
        except (TypeError, ValueError):
            return 0

    msg_max_chars = _env_lim("CHAT_MSG_MAX_CHARS")
    text_part_max_chars = _env_lim("CHAT_MSG_TEXT_PART_MAX_CHARS")

    cleaned = []
    for i, m in enumerate(other_msgs):
        if not isinstance(m, dict):
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            parts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "image_url":
                    if i == last_img_idx and len(parts) < 4:
                        parts.append(p)
                    else:
                        parts.append({"type": "text", "text": "[изображение из истории опущено для экономии токенов]"})
                elif p.get("type") == "text":
                    t = str(p.get("text", ""))
                    if text_part_max_chars and len(t) > text_part_max_chars:
                        t = t[:text_part_max_chars] + "\n...(обрезано для экономии квоты)..."
                    parts.append({"type": "text", "text": t})
                else:
                    parts.append(p)
            m = dict(m)
            m["content"] = parts
        elif isinstance(c, str):
            if msg_max_chars and len(c) > msg_max_chars:
                m = dict(m)
                m["content"] = c[:msg_max_chars] + "\n...(обрезано для экономии квоты)..."
        # Служебные ключи истории (display, attachments) не должны попадать
        # в запрос к API — оставляем только легальный набор полей.
        cleaned.append(_api_safe_msg(m))
    # Лимит количества сообщений не задан (max_msgs/max_chars = None) — отдаём
    # всю историю, как есть.
    if max_chars is None:
        return ([_api_safe_msg(system_msg)] if system_msg else []) + cleaned

    # Берём только последние ~30 сообщений — старые всё равно не влезут в лимит
    cleaned = cleaned[-30:]

    selected = []
    current_chars = _msg_len(system_msg) if system_msg else 0

    for m in reversed(cleaned):
        m_len = _msg_len(m)
        if current_chars + m_len > max_chars and selected:
            break
        if m_len > max_chars:
            m = dict(m)
            if isinstance(m.get("content"), str):
                m["content"] = m["content"][:max_chars] + "\n...(содержимое обрезано для экономии квоты)..."
            m_len = max_chars
        selected.append(m)
        current_chars += m_len

    selected.reverse()
    if system_msg:
        return [_api_safe_msg(system_msg)] + selected
    return selected

