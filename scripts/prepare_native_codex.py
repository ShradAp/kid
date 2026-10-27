"""Prepare the native prototype through the profile proxy, without launching it.

--describe is offline/read-only. --install-protection is an explicit elevated
OS-policy mutation; never call it automatically during normal app startup.
No mode falls back to an unprotected Codex executable.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import traceback
import struct
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_paths import USER_DATA_DIR
from codex_engine import PROTOCOL_VERSION
from codex_proxy import https_get
from native_codex.broker import BROKER_PORT
from native_codex.process import metadata
from native_codex.wfp import ProtectionUnavailable, describe, diagnose, install, verify
from native_codex.probe import verify_transports
from scripts.prepare_codex_runtime import private_endpoint


def prepare(profile_id, directory=USER_DATA_DIR):
    if os.name != "nt" or sys.version_info < (3, 12) or struct.calcsize("P") != 8:
        raise ProtectionUnavailable("Требуются Windows и Python 3.12+ x64")
    endpoint = private_endpoint(profile_id)
    version = PROTOCOL_VERSION
    manifest = json.loads(https_get(endpoint, "registry.npmjs.org", "/@openai%2Fcodex/" + version, max_bytes=2_000_000))
    platform = manifest["optionalDependencies"]["@openai/codex-win32-x64"]
    if not platform.startswith("npm:@openai/codex@"):
        raise ProtectionUnavailable("Неизвестный формат официального Windows-пакета")
    platform_version = platform.removeprefix("npm:@openai/codex@")
    distribution = json.loads(https_get(endpoint, "registry.npmjs.org", "/@openai%2Fcodex/" + platform_version,
                                        max_bytes=2_000_000))["dist"]
    url = urlsplit(distribution["tarball"])
    integrity = distribution["integrity"]
    if (url.scheme != "https" or url.hostname != "registry.npmjs.org" or url.query or url.fragment
            or not url.path.startswith("/@openai/codex/-/") or not integrity.startswith("sha512-")):
        raise ProtectionUnavailable("Недопустимый источник Windows-пакета")
    target = Path(directory) / "codex_native"
    target.mkdir(parents=True, exist_ok=True)
    cache = target / (version + ".tgz")
    archive = cache.read_bytes() if cache.is_file() else https_get(endpoint, "registry.npmjs.org", url.path, max_bytes=256 * 1024 * 1024, timeout=120)
    if hashlib.sha512(archive).digest() != base64.b64decode(integrity.split("-", 1)[1], validate=True):
        raise ProtectionUnavailable("Контрольная сумма Windows-пакета не совпала")
    if not cache.exists():
        with cache.open("xb") as stream:
            stream.write(archive)
    payload = None
    code_host = None
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        for member in package.getmembers():
            if member.name.endswith("/bin/codex-code-mode-host.exe") and member.isfile() and member.size < 256*1024*1024:
                code_host = package.extractfile(member).read()
            if (member.name.endswith(("/bin/codex.exe", "/codex/codex.exe"))
                    and member.isfile() and member.size < 512 * 1024 * 1024):
                if payload is not None:
                    raise ProtectionUnavailable("Неоднозначный Windows-пакет")
                payload = package.extractfile(member).read()
    if payload is None or payload[:2] != b"MZ" or code_host is None or code_host[:2] != b"MZ":
        raise ProtectionUnavailable("В пакете отсутствует Windows Codex")
    # Never execute the downloaded binary to check its version before WFP.
    # Bootstrap proof uses a private copy, not global Python's executable ID.
    python_home = Path(sys.base_prefix)
    python_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    files = {"codex.exe": payload, "network-probe.exe": Path(sys._base_executable).read_bytes(),
             python_dll: (python_home / python_dll).read_bytes(), "codex-code-mode-host.exe": code_host}
    for dependency in ("vcruntime140.dll", "vcruntime140_1.dll"):
        if (python_home / dependency).is_file():
            files[dependency] = (python_home / dependency).read_bytes()
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    for name, content in files.items():
        path = target / name
        # Do not replace an executable to which a live process may be bound.
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != hashes[name]:
            raise ProtectionUnavailable("Компоненты уже существуют с другой версией; автоматическая замена запрещена")
    for name, content in files.items():
        path = target / name
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(content)
    saved = {"version": version, "broker_port": BROKER_PORT, "sha256": hashes,
             "python_home": str(python_home), "python_dll": python_dll, "package_integrity": integrity, "active": False}
    temporary = target / "runtime.tmp"
    temporary.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    temporary.replace(target / "runtime.json")
    return {"prepared": True, "activated": False, "downloads_proxy_only": True, "codex_launched": False}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", default="codex")
    parser.add_argument("--result-file", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--describe", action="store_true")
    group.add_argument("--install-protection", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    try:
        if args.prepare:
            result = prepare(args.profile_id)
        else:
            saved = metadata(USER_DATA_DIR)
            directory = USER_DATA_DIR / "codex_native"
            executables = [directory / "codex.exe", directory / "network-probe.exe", directory / "codex-code-mode-host.exe"]
            if args.describe:
                result = {"rules": [describe(path, BROKER_PORT) for path in executables], "changes_applied": False}
            elif args.diagnose:
                result = {"policies": [diagnose(path, BROKER_PORT) for path in executables]}
            elif args.install_protection:
                result = {"protection": install(executables, BROKER_PORT), "activated": False}
            else:
                result = {"policies": [verify(path, BROKER_PORT) for path in executables],
                          "transports": verify_transports(executables[1], saved["python_home"], BROKER_PORT)}
    except Exception as error:
        if args.result_file:
            args.result_file.write_text(json.dumps({"ok": False, "error": str(error) if isinstance(error, ProtectionUnavailable)
                else type(error).__name__, "stage": traceback.extract_tb(error.__traceback__)[-1].name}, ensure_ascii=False), encoding="utf-8")
        raise
    if args.result_file:
        args.result_file.write_text(json.dumps({"ok": True, "result": result}, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if isinstance(error, ProtectionUnavailable):
            print(str(error), file=sys.stderr)
        else:
            print("Подготовка Windows Codex остановлена; прямой сетевой маршрут не используется", file=sys.stderr)
        raise SystemExit(1)
