"""Клиент чат-API: схема инструментов, вызов completions, разбор ответа."""

import json
import re
import sys
import time

# Спецсимволы для разметки блоков <think> в ответах модели
# (ранее импортировались из legacy_gui/ui_theme.py).
LT = chr(60)  # <
GT = chr(62)  # >

# Compatibility exports for API clients and runtime preparation scripts.
from tool_schemas import TOOLS_SCHEMA, WORD_TOOLS_SCHEMA

# ------------------------------------------------------------------ requests
try:
    import requests
except ImportError:
    import tkinter as _t
    from tkinter import messagebox as _mb

    _r = _t.Tk()
    _r.withdraw()
    _mb.showerror(
        "Раб",
        "Не установлена библиотека requests.\n\n"
        "python -m pip install requests keyboard pystray pillow ddgs faster-whisper\n\n"
        "Также для транскрибации видео нужен ffmpeg в PATH.\n\n"
        "Интерпретатор: " + sys.executable,
    )
    _r.destroy()
    sys.exit(1)

# Модульная сессия: TCP/TLS-соединение к провайдеру переиспользуется между
# вызовами. В tool-цикле запросов несколько подряд — keep-alive экономит
# рукопожатие на каждом после первого (раньше каждый вызов открывал
# соединение заново).
_session = requests.Session()
# LLM-API ходит НАПРЯМУЮ: env-прокси (HTTP_PROXY/HTTPS_PROXY от VPN-клиента) может
# быть мёртвым или требовать авторизации — тогда первый запрос виснет на
# read-timeout (до 120 с) до фолбэка на direct, и каждый ход «думает» аномально
# долго. Для российского API (sprutdock/OpenRouter) прокси не нужен.
_session.trust_env = False

# =====================================================================================
#                                   ЗАПРОС К API
# =====================================================================================


class ApiError(Exception):
    pass


class ToolCallWithoutToolsError(ApiError):
    """Модель вернула tool_call, хотя инструменты в запросе не передавались."""


#: Маркеры, по которым распознаём отказ провайдера из-за фильтра контента.
#: Фильтр живёт НА СТОРОНЕ API-провайдера (в приложении модерации контента нет):
#: провайдер отвечает HTTP 400 с телом вида {"error": {"message": "Content policy
#: violation."}}. Такое отклонение нужно отличать от сетевых сбоев, чтобы /api/chat
#: мог откатить «отравленный» ход из истории и чат не застревал навсегда.
_CONTENT_POLICY_MARKERS = (
    "content policy",
    "content_policy",
    "policy violation",
    "policy_violation",
    "moderation",
    "safety",
    "content filter",
    "content_filter",
    "contentfilter",
    "blocked",
    "refused",
)


def is_content_policy_error(err) -> bool:
    """True, если ошибка означает отклонение запроса фильтром контента провайдера."""
    text = str(err or "").lower()
    return any(marker in text for marker in _CONTENT_POLICY_MARKERS)


THINK_OPEN = LT + "think" + GT
THINK_CLOSE = LT + "/think" + GT
THINK_RE = re.compile(
    re.escape(THINK_OPEN) + r".*?" + re.escape(THINK_CLOSE), re.DOTALL | re.IGNORECASE
)
THINK_SPLIT_RE = re.compile(LT + r"/?think" + GT, re.IGNORECASE)


def strip_thinking(text: str) -> str:
    if not text:
        return text
    text = THINK_RE.sub("", text)
    if THINK_OPEN.lower() in text.lower():
        text = THINK_SPLIT_RE.split(text)[-1]
    return text.strip()


def call_chat_api(
    base_url, api_key, model, messages, temperature, timeout,
    tools=None, stream=False, on_thinking=None, on_content=None, cancel_event=None,
):
    # Стриминговый режим: потоковая генерация (stream=True). Возвращает тот же
    # формат, что и обычный вызов (str или dict с tool_calls), а reasoning-токены
    # доставляются через on_thinking. При отмене возвращает None.
    if stream:
        streamed = _stream_chat_completions(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            timeout=timeout,
            tools=tools,
            on_thinking=on_thinking,
            on_content=on_content,
            cancel_event=cancel_event,
        )
        if streamed is None:
            return None
        return streamed[0]

    if not api_key:
        raise ApiError(
            "Не задан API-ключ. Откройте «Профили» и впишите ключ "
            "или задайте переменную окружения из поля «Переменная окружения»."
        )
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "User-Agent": "Rab/2.0",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    # Подключаем инструменты при наличии
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # ТОЧНАЯ ПРОВЕРКА РАЗМЕРА ОТПРАВЛЯЕМЫХ ДАННЫХ
    raw_data = json.dumps(payload, ensure_ascii=False)
    char_count = len(raw_data)
    line_count = raw_data.count("\n")
    print(f"[ПРОВЕРКА ОТПРАВКИ] Символов: {char_count}, Строк переноса: {line_count}")

    # Сетевые сбои провайдера (зависание TLS-рукопожатия, отсутствие ответа)
    # ретраятся: попытки чередуют системный прокси и прямое подключение, так как
    # каналы отказывают независимо друг от друга. Connect-таймаут короткий (10с):
    # зависшее рукопожатие не должно съедать весь таймаут генерации.
    read_timeout = max(10, int(timeout))
    attempts = 3
    last_error = None
    resp = None
    for attempt in range(attempts):
        force_direct = attempt % 2 == 1  # 2-я попытка всегда напрямую, без прокси
        route = "напрямую" if force_direct else "через системный прокси"
        try:
            if force_direct:
                resp = _session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(10, read_timeout),
                    proxies={"http": None, "https": None},
                )
            else:
                resp = _session.post(url, headers=headers, json=payload, timeout=(10, read_timeout))
            last_error = None
            break
        except requests.exceptions.ReadTimeout as e:
            last_error = ApiError(
                "Сервер не ответил за %s с (%s)." % (read_timeout, route)
            )
            if attempt >= 1:
                # Ответ уже отправлен на генерацию — ретраим только один раз,
                # чтобы не платить за генерацию дважды.
                break
        except requests.exceptions.ConnectTimeout as e:
            last_error = ApiError(
                "Таймаут подключения к серверу (%s), попытка %d/%d." % (route, attempt + 1, attempts)
            )
        except requests.exceptions.SSLError as e:
            last_error = ApiError("Ошибка SSL (%s): %s" % (route, e))
        except requests.exceptions.ConnectionError as e:
            last_error = ApiError(
                "Нет соединения (%s). Проверьте интернет и адрес API." % route
            )
        except Exception as e:
            last_error = ApiError("Ошибка сети (%s): %s: %s" % (route, type(e).__name__, e))
        time.sleep(0.8 * (attempt + 1))

    if last_error is not None:
        raise ApiError(
            "API недоступен после %d попыток (прокси и напрямую). %s\n"
            "Проверьте VPN/прокси и доступность %s" % (attempts, last_error, base_url)
        )

    if resp.status_code in (401, 403):
        raise ApiError(
            "HTTP %d: неверный или просроченный API-ключ." % resp.status_code
        )
    if resp.status_code == 402:
        raise ApiError(
            "HTTP 402: Недостаточно средств на балансе API (Insufficient quota). Пополните баланс аккаунта."
        )
    if resp.status_code == 404:
        raise ApiError("HTTP 404: неверный адрес API или модель.")
    if resp.status_code == 429:
        raise ApiError("HTTP 429: превышен лимит запросов.")
    if resp.status_code >= 500:
        raise ApiError("Сервер вернул ошибку %d." % resp.status_code)
    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            detail = resp.json().get("error", {}).get("message") or detail
        except Exception:
            pass
        low = (detail or "").lower()
        if (
            "image" in low
            or "modal" in low
            or "vision" in low
            or "unsupported image" in low
        ):
            raise ApiError(
                "Модель или провайдер не принимает изображения.\n"
                "Выберите vision-модель (например, qwen-vl) в профиле.\n"
                "Ответ сервера: " + detail[:200]
            )
        raise ApiError("HTTP %d: %s" % (resp.status_code, detail))

    try:
        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message") or {}
    except Exception:
        raise ApiError("Некорректный ответ сервера:\n" + resp.text[:300])

    tool_calls = msg.get("tool_calls") or []

    # Если модель запросила вызов инструментов — возвращаем объект вызова.
    # Но если инструменты в запросе отключены, это служебный ответ: пытаемся
    # вытащить текст, иначе отдаём понятную ошибку вместо сырого dict в UI.
    if tool_calls:
        if tools:
            return msg
        fallback_text = (
            msg.get("content")
            or msg.get("reasoning_content")
            or choice.get("text")
            or ""
        )
        if isinstance(fallback_text, list):
            parts = []
            for p in fallback_text:
                if isinstance(p, dict):
                    t = p.get("text")
                    if t:
                        parts.append(str(t))
            fallback_text = "\n".join(parts)
        fallback_text = strip_thinking(str(fallback_text or ""))
        if fallback_text:
            return fallback_text
        raise ToolCallWithoutToolsError(
            "Модель вернула служебный tool-вызов вместо текста. "
            "Повторите запрос (или включите интернет-поиск, если он нужен)."
        )

    content = (
        msg.get("content") or msg.get("reasoning_content") or choice.get("text") or ""
    )
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if t:
                    parts.append(str(t))
        content = "\n".join(parts)
    content = strip_thinking(str(content or ""))
    if not content:
        raise ApiError("Модель вернула пустой ответ.")
    return content


def _raise_http_error(resp):
    """Общая карта HTTP-ошибок для стримингового вызова."""
    if resp.status_code in (401, 403):
        raise ApiError(
            "HTTP %d: неверный или просроченный API-ключ." % resp.status_code
        )
    if resp.status_code == 402:
        raise ApiError(
            "HTTP 402: Недостаточно средств на балансе API (Insufficient quota). Пополните баланс аккаунта."
        )
    if resp.status_code == 404:
        raise ApiError("HTTP 404: неверный адрес API или модель.")
    if resp.status_code == 429:
        raise ApiError("HTTP 429: превышен лимит запросов.")
    if resp.status_code >= 500:
        raise ApiError("Сервер вернул ошибку %d." % resp.status_code)
    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            detail = resp.json().get("error", {}).get("message") or detail
        except Exception:
            pass
        low = (detail or "").lower()
        if (
            "image" in low
            or "modal" in low
            or "vision" in low
            or "unsupported image" in low
        ):
            raise ApiError(
                "Модель или провайдер не принимает изображения.\n"
                "Выберите vision-модель (например, qwen-vl) в профиле.\n"
                "Ответ сервера: " + detail[:200]
            )
        raise ApiError("HTTP %d: %s" % (resp.status_code, detail))



def _stream_chat_completions(
    base_url, api_key, model, messages, temperature, timeout,
    tools=None, on_thinking=None, on_content=None, cancel_event=None,
):
    """Стриминговый вызов chat/completions (stream=True).

    Возвращает кортеж (result, reasoning_text):
      - result — тот же формат, что и у call_chat_api: str (текст) либо dict
        (сообщение с tool_calls и reasoning_content). Совместимо с текущими
        вызывающими кодами;
      - reasoning_text — накопленный текст «размышлений» модели (reasoning content
        / thought parts), пришедший за этот вызов (может быть пустой строкой).

    on_thinking(delta_text) вызывается по мере поступления reasoning-токенов,
    on_content(delta_text) — content-токенов. Оба опциональны и не должны бросать.
    При отмене (cancel_event установлен) возвращает None.
    """
    if not api_key:
        raise ApiError(
            "Не задан API-ключ. Откройте «Профили» и впишите ключ "
            "или задайте переменную окружения из поля «Переменная окружения»."
        )
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "User-Agent": "Rab/2.0",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # Та же retry-схема, что и в call_chat_api: попытки чередуют системный прокси
    # и прямое подключение. При стриме чтение идёт потоком, поэтому read-таймаут
    # относится к задержке между чанками, а не ко всей генерации.
    read_timeout = max(10, int(timeout))
    attempts = 3
    last_error = None
    resp = None
    for attempt in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            return None
        force_direct = attempt % 2 == 1
        route = "напрямую" if force_direct else "через системный прокси"
        try:
            if force_direct:
                resp = _session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(10, read_timeout),
                    stream=True,
                    proxies={"http": None, "https": None},
                )
            else:
                resp = _session.post(
                    url, headers=headers, json=payload, timeout=(10, read_timeout), stream=True
                )
            last_error = None
            break
        except requests.exceptions.ReadTimeout as e:
            last_error = ApiError(
                "Сервер не ответил за %s с (%s)." % (read_timeout, route)
            )
            if attempt >= 1:
                break
        except requests.exceptions.ConnectTimeout as e:
            last_error = ApiError(
                "Таймаут подключения к серверу (%s), попытка %d/%d." % (route, attempt + 1, attempts)
            )
        except requests.exceptions.SSLError as e:
            last_error = ApiError("Ошибка SSL (%s): %s" % (route, e))
        except requests.exceptions.ConnectionError as e:
            last_error = ApiError(
                "Нет соединения (%s). Проверьте интернет и адрес API." % route
            )
        except Exception as e:
            last_error = ApiError("Ошибка сети (%s): %s: %s" % (route, type(e).__name__, e))
        time.sleep(0.8 * (attempt + 1))

    if last_error is not None:
        raise ApiError(
            "API недоступен после %d попыток (прокси и напрямую). %s\n"
            "Проверьте VPN/прокси и доступность %s" % (attempts, last_error, base_url)
        )

    _raise_http_error(resp)

    content_type = (resp.headers.get("Content-Type") or "").lower()
    is_sse = "text/event-stream" in content_type

    reasoning_parts = []
    content_parts = []
    tool_calls_acc = {}

    def _emit_reasoning(text):
        if not text:
            return
        reasoning_parts.append(text)
        if on_thinking is not None:
            try:
                on_thinking(text)
            except Exception:
                pass

    def _emit_content(text):
        if not text:
            return
        content_parts.append(text)
        if on_content is not None:
            try:
                on_content(text)
            except Exception:
                pass

    def _collect(obj):
        """Достаёт reasoning и content из delta/message независимо от схемы провайдера."""
        if not isinstance(obj, dict):
            return
        for key in ("reasoning_content", "reasoning", "thinking", "thought"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                _emit_reasoning(val)
        content = obj.get("content")
        if isinstance(content, str):
            if content:
                _emit_content(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                ptext = part.get("text")
                pthink = part.get("thinking")
                preason = part.get("reasoning")
                if ptype in ("thinking", "reasoning", "thought") or (ptext is None and (pthink or preason)):
                    candidate = pthink or preason or ptext
                    if isinstance(candidate, str) and candidate:
                        _emit_reasoning(candidate)
                elif isinstance(ptext, str) and ptext:
                    _emit_content(ptext)

    try:
        if is_sse:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    return None
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                _collect(delta)
                # Некоторые провайдеры шлют первым чанком message, а не delta.
                msg = choice.get("message")
                if isinstance(msg, dict):
                    _collect(msg)
                dtc = delta.get("tool_calls") or []
                for tc in dtc:
                    if not isinstance(tc, dict):
                        continue
                    idx = int(tc.get("index", 0))
                    acc = tool_calls_acc.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        acc["id"] = str(tc["id"])
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] = str(fn["name"])
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += str(fn["arguments"])
        else:
            # Провайдер проигнорировал stream — читаем обычный JSON и разбираем его.
            try:
                data = resp.json()
                choice = data["choices"][0]
                msg = choice.get("message") or {}
            except Exception:
                raise ApiError("Некорректный ответ сервера:\n" + resp.text[:300])
            _collect(msg)
            _collect(choice)
            tool_calls = msg.get("tool_calls") or []
            for idx, tc in enumerate(tool_calls):
                if isinstance(tc, dict):
                    tool_calls_acc[idx] = {
                        "id": str(tc.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str((tc.get("function") or {}).get("name") or ""),
                            "arguments": str((tc.get("function") or {}).get("arguments") or ""),
                        },
                    }
    finally:
        try:
            resp.close()
        except Exception:
            pass

    reasoning_text = "".join(reasoning_parts)
    content_text = "".join(content_parts)

    tool_calls = [
        tool_calls_acc[idx]
        for idx in sorted(tool_calls_acc)
        if tool_calls_acc[idx]["function"]["name"] or tool_calls_acc[idx]["id"]
    ]

    if tool_calls:
        if tools:
            return (
                {
                    "role": "assistant",
                    "content": content_text,
                    "tool_calls": tool_calls,
                    "reasoning_content": reasoning_text,
                },
                reasoning_text,
            )
        fallback_text = strip_thinking(content_text or reasoning_text)
        if fallback_text:
            return (fallback_text, reasoning_text)
        raise ToolCallWithoutToolsError(
            "Модель вернула служебный tool-вызов вместо текста. "
            "Повторите запрос (или включите интернет-поиск, если он нужен)."
        )

    # <think>...</think> не вырезаем здесь: это делает вызывающий код (desktop_app),
    # чтобы размышления, вложенные моделью прямо в content, не терялись.
    content_text = content_text or ""
    if not content_text:
        if reasoning_text:
            return ("", reasoning_text)
        raise ApiError("Модель вернула пустой ответ.")
    return (content_text, reasoning_text)
