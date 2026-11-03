"""Explicit owner-only network diagnostic; never creates fake updates or runs Codex."""

import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send and edit one ordinary preview, then deliver a final message")
    args = parser.parse_args()
    if not args.send:
        print("No network calls. Use --send for the explicit Telegram preview diagnostic.")
        return
    from app_paths import USER_DATA_DIR
    from services.telegram_storage import TelegramStorage
    from services.telegram_transport import TelegramTransport
    settings = TelegramStorage(USER_DATA_DIR).settings()
    if not settings["token"] or not settings["owner_id"]:
        raise SystemExit("Configure the bot and owner first")
    client = TelegramTransport(settings, USER_DATA_DIR)
    try:
        message_id = None
        for step in range(1, 4):
            text = f"Тест постепенного появления: {step}/3.\n" + "Текст добавляется. " * step
            if message_id is None:
                result = client.call("sendMessage", chat_id=int(settings["owner_id"]), text=text, disable_notification=True)
                message_id = result["message_id"]
            else:
                result = client.call("editMessageText", chat_id=int(settings["owner_id"]), message_id=message_id, text=text)
                assert result["message_id"] == message_id
            time.sleep(1.6)
        client.call("editMessageText", chat_id=int(settings["owner_id"]), message_id=message_id, text="Проверка предпросмотра завершена.")
        receipt = client.call("sendRichMessage", chat_id=int(settings["owner_id"]),
                              rich_message={"html": "<p><b>Проверка предпросмотра завершена.</b> Обычное сообщение обновлено, готовый ответ доставлен отдельно. Проверьте прокрутку на телефоне.</p>"})
        assert receipt.get("message_id")
        print("PASS: one ordinary preview edited and finalized; rich final answer delivered; no ephemeral drafts or polling offset changes")
    finally:
        client.close_thread()


if __name__ == "__main__":
    main()
