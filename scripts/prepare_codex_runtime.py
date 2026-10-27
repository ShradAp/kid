"""Prepare an immutable local Codex image; ALL downloads use the chosen proxy.

No docker pull/build is used. A suitable Python 3.11+ Linux amd64 base image
must already exist locally. No account login or inference is performed here.
"""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_paths import USER_DATA_DIR
from codex_proxy import ProxyEndpoint, ProxyUnavailable, https_get
from codex_runtime import IsolatedRuntime, IsolationUnavailable, runtime_sources, source_fingerprint


def private_endpoint(profile_id=None, use_app_proxy=False):
    try:
        config = json.loads((USER_DATA_DIR / "config.json").read_text(encoding="utf-8"))
        if use_app_proxy:
            url = urlsplit(config.get("proxy_url", ""))
            scheme = "socks5h" if url.scheme == "socks5" else url.scheme
            username = config.get("proxy_user") or url.username or ""
            authority = str(url.hostname)
            if ":" in authority:
                authority = "[" + authority + "]"
            endpoint = f"{scheme}://{quote(username, safe='')}@{authority}:{url.port}"
            return ProxyEndpoint.parse(endpoint, config.get("proxy_pass", ""))
        value = config["profiles"][profile_id]["codex"]
        return ProxyEndpoint.parse(value.get("proxy", ""), value.get("proxy_password", ""))
    except Exception:
        raise IsolationUnavailable("Настройте числовой IP прокси и пароль в выбранном профиле") from None


def prepare(endpoint, base_image, version):
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise IsolationUnavailable("Некорректная версия")
    runtime_root = USER_DATA_DIR / "codex_runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime = IsolatedRuntime(USER_DATA_DIR, runtime_root / "unused", endpoint)
    base = json.loads(runtime.docker("image", "inspect", base_image))[0]
    if base["Os"] != "linux" or base["Architecture"] != "amd64" or base["Config"].get("Volumes"):
        raise IsolationUnavailable("Требуется локальный Python Linux amd64 образ без VOLUME")
    base_id = base["Id"]
    manifest = json.loads(https_get(endpoint, "registry.npmjs.org", "/@openai%2Fcodex/" + version, max_bytes=2_000_000))
    platform = manifest["optionalDependencies"]["@openai/codex-linux-x64"]
    prefix = "npm:@openai/codex@"
    if not platform.startswith(prefix):
        raise IsolationUnavailable("Неожиданный формат официального платформенного пакета")
    platform_version = platform[len(prefix):]
    platform_manifest = json.loads(https_get(endpoint, "registry.npmjs.org", "/@openai%2Fcodex/" + platform_version, max_bytes=2_000_000))
    distribution = platform_manifest["dist"]
    url = urlsplit(distribution["tarball"])
    integrity = distribution["integrity"]
    if (url.scheme != "https" or url.hostname != "registry.npmjs.org" or url.query or url.fragment
            or not url.path.startswith("/@openai/codex/-/") or not integrity.startswith("sha512-")):
        raise IsolationUnavailable("Неожиданный источник официального пакета")
    cache = runtime_root / (version + ".tgz")
    archive = cache.read_bytes() if cache.exists() else https_get(endpoint, "registry.npmjs.org", url.path, max_bytes=256 * 1024 * 1024, timeout=120)
    expected = base64.b64decode(integrity.split("-", 1)[1], validate=True)
    if hashlib.sha512(archive).digest() != expected:
        raise IsolationUnavailable("Контрольная сумма пакета Codex не совпала")
    cache.write_bytes(archive)
    files = {}
    wanted = {"bin/codex", "bin/codex-code-mode-host", "codex-resources/bwrap", "codex-path/rg", "codex-package.json"}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        for entry in package.getmembers():
            relative = "/".join(entry.name.split("/")[3:])
            if entry.isfile() and relative in wanted:
                if entry.size > 300 * 1024 * 1024:
                    raise IsolationUnavailable("Слишком большой бинарный файл")
                files[relative] = package.extractfile(entry).read()
    if "bin/codex" not in files:
        raise IsolationUnavailable("В пакете нет Linux-бинарного файла Codex")
    worker = (ROOT / "codex_worker.py").read_bytes()
    sources = runtime_sources()
    digest = hashlib.sha256()
    for name, content in sorted(sources.items()):
        digest.update(name.encode())
        digest.update(hashlib.sha256(content).digest())
    prepared_fingerprint = digest.hexdigest()
    files.update(sources)
    import api_client
    files["kid_tools.json"] = json.dumps([{"name": item["function"]["name"], "description": item["function"]["description"],
                                          "inputSchema": item["function"]["parameters"]} for item in api_client.TOOLS_SCHEMA]).encode("utf-8")
    files["deny-browser"] = b"#!/bin/sh\nexit 126\n"
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w") as output:
        directories = {"kid"}
        for name in files:
            directories.update(str(parent).replace("\\", "/") for parent in Path("kid", name).parents if str(parent) != ".")
        for name in sorted(directories):
            directory = tarfile.TarInfo(name)
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            output.addfile(directory)
        for name, content in files.items():
            entry = tarfile.TarInfo("kid/" + name)
            entry.size = len(content)
            entry.mode = 0o644 if name.endswith(".py") else 0o755
            output.addfile(entry, io.BytesIO(content))
    container = runtime.docker("create", "--pull", "never", "--network", "none", "--entrypoint", "/usr/local/bin/python", base_id, "-c", "pass").strip()
    try:
        # cp/commit operate on a stopped container: no installer process or
        # inherited entrypoint can access the Internet, even accidentally.
        result = subprocess.run(runtime.docker_command("cp", "-", container + ":/opt"), input=bundle.getvalue(),
                                capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise IsolationUnavailable("Не удалось записать файлы защищённого образа")
        image_id = runtime.docker("commit", container, "kid-codex:" + version, timeout=120).strip()
        version_output = runtime.docker("run", "--rm", "--pull", "never", "--network", "none", "--cap-drop", "ALL",
                                       "--security-opt", "no-new-privileges", "--user", "1000:1000", "--read-only",
                                       "--entrypoint", "/opt/kid/bin/codex", image_id, "--version")
        if ("codex-cli " + version) not in version_output:
            raise IsolationUnavailable("Версия защищённого бинарного файла не совпала")
        if source_fingerprint() != prepared_fingerprint:
            raise IsolationUnavailable("Исходники изменились во время подготовки; повторите подготовку образа")
        metadata = {"image_id": image_id, "version": version, "worker_sha256": hashlib.sha256(worker).hexdigest(),
                    "source_sha256": prepared_fingerprint,
                    "package_integrity": integrity, "base_image_id": base_id}
        temporary = runtime_root / "runtime.tmp"
        temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temporary.replace(runtime_root / "runtime.json")
        return {"image_prepared": True, "version": version, "downloads_proxy_only": True}
    finally:
        runtime.docker("rm", "--force", container)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--profile-id")
    selection.add_argument("--use-app-proxy", action="store_true")
    parser.add_argument("--base-image", default="ghcr.io/open-webui/open-webui:main")
    parser.add_argument("--version", default="0.164.0-alpha.11.1")
    arguments = parser.parse_args()
    try:
        endpoint = private_endpoint(arguments.profile_id, arguments.use_app_proxy)
        print(json.dumps(prepare(endpoint, arguments.base_image, arguments.version)))
    except (IsolationUnavailable, ProxyUnavailable) as error:
        print(str(error))
        raise SystemExit(1)
    except Exception as error:
        # No urllib/socket/package/Docker errors containing credentials.
        print("Runtime setup blocked: " + type(error).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
