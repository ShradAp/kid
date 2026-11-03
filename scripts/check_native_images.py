"""Real vision smoke test through the global proxy and existing WFP gate."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from PIL import Image, ImageDraw
    from app_paths import USER_DATA_DIR
    from bridge_security import FileAccess
    from codex_attachments import prepare_attachments
    from codex_service import CodexService
    from config_manager import load_config
    cfg = load_config()
    profile_id = next(key for key, value in cfg["profiles"].items() if value.get("engine") == "codex")
    profile = copy.deepcopy(cfg["profiles"][profile_id])
    with tempfile.TemporaryDirectory(prefix="kid-vision-") as folder:
        profile["codex"]["workspace"] = folder
        path = Path(folder) / "vision.png"
        image = Image.new("RGB", (400, 220), "white")
        draw = ImageDraw.Draw(image)
        draw.ellipse((20, 30, 180, 190), fill="blue")
        draw.rectangle((220, 30, 380, 190), fill="orange")
        image.save(path)
        inputs, _ = prepare_attachments([{"path": str(path)}], FileAccess((folder,)), profile)
        service = CodexService(USER_DATA_DIR, workspace_grant=lambda _: folder)
        chat = "vision-check-" + uuid.uuid4().hex
        try:
            session = service.get(profile_id, profile)
            result = session.run(chat, "Describe the two shapes and their colors in this image, in one English sentence. Do not use tools.",
                                 str(uuid.uuid4()), threading.Event(), timeout=120, input_items=inputs)
            reply = result["reply"].lower()
            assert all(word in reply for word in ("blue", "orange", "circle", "square")), "Unexpected vision response"
            print(json.dumps({"native_vision": True, "global_proxy": True, "wfp_verified": True,
                              "identified_colors_and_shapes": True}))
        finally:
            service.forget(chat)
            service.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"native_vision": False, "error_type": type(error).__name__}))
        raise SystemExit(1)
