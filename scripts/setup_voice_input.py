"""Prepare the small offline Russian speech model (one-time download)."""
import hashlib
from pathlib import Path
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_paths import USER_DATA_DIR
from services.voice_input import MODEL_NAME


def main():
    import requests
    destination = USER_DATA_DIR / "voice_models"
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / MODEL_NAME / "am" / "final.mdl").is_file():
        print("Russian model is ready.", flush=True)
        return
    archive = destination / (MODEL_NAME + ".download")
    started = time.monotonic()
    try:
        url = ("https://huggingface.co/rhasspy/vosk-models/resolve/"
               "e7ac2109d134b5f2404ba95389b2fb51916d4cab/ru/" + MODEL_NAME + ".zip")
        digest = hashlib.sha256()
        with requests.get(url,
                          stream=True, timeout=(15, 20)) as response:
            response.raise_for_status()
            size = 0
            with archive.open("wb") as output:
                for chunk in response.iter_content(1024 * 256):
                    if time.monotonic() - started > 240:
                        raise TimeoutError("Model download exceeded four minutes")
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size % (1024 * 1024) == 0:
                        print(f"Downloaded {size // (1024 * 1024)} MB", flush=True)
        expected = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
        if digest.hexdigest() != expected:
            raise RuntimeError("Speech model checksum mismatch")
        with zipfile.ZipFile(archive) as model_zip:
            for item in model_zip.infolist():
                target = (destination / item.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError("Invalid model archive path")
            model_zip.extractall(destination)
        if not (destination / MODEL_NAME / "am" / "final.mdl").is_file():
            raise RuntimeError("Model archive is incomplete")
        print("Russian model is ready.", flush=True)
    finally:
        archive.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
