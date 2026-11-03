"""Validate rich formatting offline, optionally send a preview to the configured owner."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.telegram_format import format_reply


SAMPLE = r"""# KID: проверка оформления

**Жирный текст**, _курсив_, ~~зачёркнутый~~ и `код`.

| Возможность | Пример |
|---|---:|
| Таблица | **работает** |
| Формула | $E=mc^2$ |

\[\frac{a}{b}=\sqrt{x}\]

```python
print("код <сохраняется> без изменений")
```

- Первый пункт
- Второй пункт
"""


def assert_cyrillic(value):
    # ASCII escapes intentionally survive even a misconfigured Windows stdin.
    expected = ("\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0438\u044f",
                "\u0416\u0438\u0440\u043d\u044b\u0439 \u0442\u0435\u043a\u0441\u0442",
                "\u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442",
                "\u0441\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u0442\u0441\u044f")
    serialized = json.dumps(value, ensure_ascii=False)
    if not all(text in serialized for text in expected):
        raise AssertionError("Cyrillic text was lost before sending or in the Telegram response")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send the formatting preview to the configured Telegram owner")
    args = parser.parse_args()
    chunks = list(format_reply(SAMPLE))
    assert_cyrillic(chunks)
    assert any("<table" in item["rich_html"] for item in chunks)
    assert any("<tg-math-block>" in item["rich_html"] for item in chunks)
    if not args.send:
        print("PASS: balanced rich HTML and Cyrillic preserved; no network calls")
        return
    from app_paths import USER_DATA_DIR
    from services.telegram_storage import TelegramStorage
    from services.telegram_transport import TelegramTransport
    settings = TelegramStorage(USER_DATA_DIR).settings()
    if not settings["token"] or not settings["owner_id"]:
        raise SystemExit("Configure the Telegram token and owner first")
    client = TelegramTransport(settings, USER_DATA_DIR)
    try:
        receipts = []
        for item in chunks:
            receipt = client.call("sendRichMessage", chat_id=int(settings["owner_id"]), rich_message={"html": item["rich_html"]})
            receipts.append(receipt)
            types = [block.get("type") for block in (receipt.get("rich_message") or {}).get("blocks", [])]
            assert "table" in types and "mathematical_expression" in types, types
            print(json.dumps({"delivered": bool(receipt.get("message_id")), "native_blocks": types}, ensure_ascii=True))
        assert_cyrillic(receipts)
        print("PASS: Telegram returned the original Cyrillic text in native message blocks")
    finally:
        client.close_thread()


if __name__ == "__main__":
    main()
