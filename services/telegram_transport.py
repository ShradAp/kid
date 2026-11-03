"""Small Bot API client. No ambient proxy, secret-bearing errors or redirects."""

import requests
import threading
from pathlib import Path, PurePosixPath
import re
from contextlib import ExitStack


class TelegramError(RuntimeError):
    def __init__(self, message="Telegram недоступен", retry_after=5, *, status_code=None, format_rejected=False, not_modified=False, message_missing=False):
        super().__init__(message)
        self.retry_after = max(1, min(int(retry_after), 300))
        self.status_code = status_code
        self.format_rejected = format_rejected
        self.not_modified = not_modified
        self.message_missing = message_missing


class TelegramTransport:
    def __init__(self, settings, directory):
        self.token = settings["token"]
        self.directory = directory
        self.network = settings.get("network", "direct")
        self.local = threading.local()

    def session(self):
        # Keep CONNECT/TLS alive without sharing a requests.Session across threads.
        client = getattr(self.local, "client", None)
        if client is None:
            client = requests.Session()
            client.trust_env = False
            self.local.client = client
        return client

    def close_thread(self):
        client = getattr(self.local, "client", None)
        if client is not None:
            client.close()
            del self.local.client

    def proxies(self):
        from urllib.parse import quote

        proxies = {}
        if self.network == "proxy":
            try:
                from codex_proxy import global_endpoint
                endpoint = global_endpoint(self.directory)
                host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
                auth = (quote(endpoint.username, safe="") + ":" + quote(endpoint.password, safe="") + "@"
                        if endpoint.username or endpoint.password else "")
                address = f"{endpoint.scheme}://{auth}{host}:{endpoint.port}"
                proxies = {"http": address, "https": address}
            except Exception:
                raise TelegramError("Прокси Telegram недоступен") from None
        return proxies

    def call(self, method, **parameters):
        proxies = self.proxies()
        try:
            client = self.session()
            response = None
            with ExitStack() as files:
                if method == "sendDocument":
                    path = Path(parameters.pop("document_path"))
                    if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
                        raise TelegramError("Файл недоступен или превышает 50 МБ")
                    name = parameters.pop("filename", path.name)
                    document = files.enter_context(path.open("rb"))
                    request = {"data": parameters, "files": {"document": (name, document, "application/octet-stream")}}
                else:
                    request = {"json": parameters}
                response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", **request,
                                       proxies=proxies, timeout=(5, 90 if method == "sendDocument" else 20), allow_redirects=False)
                files.callback(response.close)
                if method in ("editMessageText", "sendRichMessageDraft", "sendMessageDraft") and response.status_code in (400, 404):
                    description = str(response.json().get("description") or "").lower()
                    raise TelegramError("Telegram отклонил обновление предпросмотра или панели",
                                        status_code=response.status_code,
                                        not_modified="message is not modified" in description,
                                        message_missing="message to edit not found" in description)
                if method == "sendRichMessage" and response.status_code in (400, 404):
                    raise TelegramError("Telegram отклонил расширенное форматирование", status_code=response.status_code, format_rejected=True)
                if response.status_code == 429:
                    data = response.json()
                    raise TelegramError("Telegram ограничил частоту запросов", data.get("parameters", {}).get("retry_after", 5))
                if response.status_code in (401, 404):
                    raise TelegramError("Проверьте токен бота Telegram", 60)
                if response.status_code == 409:
                    raise TelegramError("Бот уже используется другим процессом или webhook", 30)
                if response.status_code == 403:
                    raise TelegramError("Бот заблокирован или не может написать владельцу", 60)
                if response.status_code != 200:
                    raise TelegramError()
                data = response.json()
                if not data.get("ok"):
                    raise TelegramError("Telegram отклонил запрос")
                return data["result"]
        except TelegramError:
            raise
        except Exception:
            # requests exceptions contain the full URL, including the bot token.
            raise TelegramError() from None

    def download(self, file_path, destination, cancel, max_bytes=20 * 1024 * 1024):
        # Accept only the relative file_path returned by getFile, never a URL.
        if (not isinstance(file_path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", file_path)
                or PurePosixPath(file_path).is_absolute() or ".." in PurePosixPath(file_path).parts):
            raise TelegramError("Некорректный путь файла Telegram")
        destination = Path(destination)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with self.session().get(f"https://api.telegram.org/file/bot{self.token}/{file_path}",
                                    proxies=self.proxies(), stream=True, timeout=(5, 60), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise TelegramError("Не удалось скачать файл Telegram")
                if int(response.headers.get("Content-Length", 0)) > max_bytes:
                    raise TelegramError("Файл Telegram превышает 20 МБ")
                size = 0
                with temporary.open("xb") as output:
                    for chunk in response.iter_content(64 * 1024):
                        if cancel.is_set():
                            raise TelegramError("Загрузка файла остановлена")
                        size += len(chunk)
                        if size > max_bytes:
                            raise TelegramError("Файл Telegram превышает 20 МБ")
                        output.write(chunk)
                if cancel.is_set():
                    raise TelegramError("Загрузка файла остановлена")
                temporary.replace(destination)
        except TelegramError:
            raise
        except Exception:
            raise TelegramError("Не удалось скачать файл Telegram") from None
        finally:
            temporary.unlink(missing_ok=True)


def text_chunks(text, limit=3500):
    """Bound by UTF-16 units as well as code points (emoji consume two units)."""
    text = str(text or "Ответ без текста")
    chunk, size = [], 0
    for character in text:
        units = 2 if ord(character) > 0xffff else 1
        if size + units > limit:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(character)
        size += units
    if chunk:
        yield "".join(chunk)
