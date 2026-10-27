"""Local project inspector and closed Windows actions; no model-supplied shell."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading

MAX_TEXT_BYTES = 512 * 1024
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".next", "__pycache__", ".backups"}


def windows_desktop():
    if os.name != "nt":
        raise ValueError("Рабочий стол Windows недоступен")
    import ctypes
    buffer=ctypes.create_unicode_buffer(32768)
    # CSIDL_DESKTOPDIRECTORY follows the actual Windows/OneDrive redirection.
    if ctypes.windll.shell32.SHGetFolderPathW(None,0x10,None,0,buffer) != 0 or not buffer.value:
        raise ValueError("Windows не вернула путь к рабочему столу")
    return Path(buffer.value)


def windows_diagnostic(command):
    """Closed command vocabulary; never shells out with model-supplied code."""
    commands = {"ipconfig":[], "ipconfig /all":["/all"]}
    if command not in commands or os.name != "nt":
        raise ValueError("Эта команда Windows заблокирована. Доступны только ipconfig и ipconfig /all; произвольный shell требует изоляции сети ОС.")
    import ctypes
    system = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system)):
        raise OSError("Каталог Windows недоступен")
    binary = Path(system.value) / "ipconfig.exe"
    result = subprocess.run([str(binary), *commands[command]], cwd=system.value, shell=False,
                            capture_output=True, timeout=15, creationflags=0x08000000)
    encoding = "cp" + str(ctypes.windll.kernel32.GetOEMCP())
    return {"ok":result.returncode == 0, "platform":"Windows", "command":command,
            "stdout":result.stdout[:128*1024].decode(encoding, errors="replace"),
            "stderr":result.stderr[:8192].decode(encoding, errors="replace"), "exit_code":result.returncode}


def create_windows_file(root, file_name, content):
    """Exclusive creation in a verified local directory, without active formats."""
    import io
    import re
    import zipfile
    from xml.sax.saxutils import escape
    if (not isinstance(file_name, str) or not file_name or len(file_name)>200
            or re.search(r'[<>:"/\\|?*\x00-\x1f]',file_name) or file_name != file_name.strip()
            or file_name.endswith(("."," ")) or file_name.split(".")[0].upper() in
            {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(10)),*(f"LPT{i}" for i in range(10))}):
        raise ValueError("Требуется обычное имя файла без пути и специальных символов")
    suffix=Path(file_name).suffix.lower()
    if suffix not in {".txt",".md",".csv",".json",".docx"}:
        raise ValueError("Допустимы txt, md, csv, json, docx; исполняемые форматы заблокированы")
    if not isinstance(content,str) or len(content.encode("utf-8"))>2*1024*1024:
        raise ValueError("Содержимое превышает 2 МиБ")
    check=CodeWorkspace()
    folder=Path(check.grant("host",root))
    target=folder/file_name
    if target.exists() or target.is_symlink() or target.is_junction():
        raise ValueError("Файл уже существует; автоматическая перезапись запрещена")
    payload=content.encode("utf-8")
    if suffix==".docx":
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml",'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr("_rels/.rels",'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            paragraphs="".join('<w:p><w:r><w:t xml:space="preserve">'+escape(line)+'</w:t></w:r></w:p>' for line in content.split("\n"))
            archive.writestr("word/document.xml",'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+paragraphs+'</w:body></w:document>')
        payload=output.getvalue()
    created=False
    try:
        with target.open("xb") as stream:
            created=True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    return {"ok":True,"platform":"Windows","path":str(target),"size_bytes":len(payload)}


class CodeWorkspace:
    def __init__(self):
        # Grants are deliberately not restored from untrusted profile JSON.
        self.roots = {}
        self.lock = threading.RLock()

    def grant(self, profile_id, path):
        requested = Path(path)
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("Выберите локальную рабочую папку")
        if os.name == "nt":
            import ctypes
            if not requested.is_absolute() or ctypes.windll.kernel32.GetDriveTypeW(requested.anchor) != 3:
                raise ValueError("Требуется локальный фиксированный диск")
            for component in reversed((requested, *requested.parents)):
                if component.is_symlink() or component.is_junction():
                    raise ValueError("Папки через ссылки и junction запрещены")
        candidate = requested.resolve(strict=True)
        if not candidate.is_dir():
            raise ValueError("Рабочая папка не найдена")
        if str(candidate).startswith(("\\\\", "//")):
            raise ValueError("Выберите локальную рабочую папку")
        if os.name == "nt":
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(candidate.anchor) == 4:
                raise ValueError("Сетевые диски не поддерживаются")
        with self.lock:
            self.roots[str(profile_id)] = candidate
        return str(candidate)

    def root(self, profile_id):
        with self.lock:
            root = self.roots.get(str(profile_id))
        if root is None:
            raise ValueError("Выберите рабочую папку в этом запуске приложения")
        return root

    def file(self, profile_id, relative):
        root = self.root(profile_id)
        requested = Path(relative)
        if requested.is_absolute() or requested.drive or ".." in requested.parts or str(relative).startswith(("\\\\", "//")):
            raise ValueError("Файл за пределами рабочей папки")
        cursor = root
        for part in requested.parts:
            cursor = cursor / part
            if cursor.is_symlink() or cursor.is_junction():
                raise ValueError("Ссылки и junction в путях проекта запрещены")
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Файл за пределами рабочей папки")
        if any(part in SKIP_DIRS for part in target.relative_to(root).parts):
            raise ValueError("Служебный каталог недоступен")
        return target

    def files(self, profile_id):
        root = self.root(profile_id)
        result, truncated = [], False
        visited = 0
        for directory, folders, names in os.walk(root, followlinks=False):
            visited += 1
            if visited > 1000:
                truncated = True
                break
            folders[:] = sorted(name for name in folders if name not in SKIP_DIRS
                                and not (Path(directory) / name).is_symlink()
                                and not (Path(directory) / name).is_junction())
            for name in sorted(names):
                target = Path(directory) / name
                if target.is_symlink() or not target.resolve().is_relative_to(root):
                    continue
                result.append(target.relative_to(root).as_posix())
                if len(result) >= 500:
                    truncated = True
                    break
            if truncated:
                break
        return {"root": str(root), "files": result, "truncated": truncated}

    def preview(self, profile_id, relative):
        target = self.file(profile_id, relative)
        with target.open("rb") as stream:
            raw = stream.read(MAX_TEXT_BYTES + 1)
        if b"\0" in raw:
            raise ValueError("Двоичный файл: текстовый просмотр недоступен")
        try:
            text = raw[:MAX_TEXT_BYTES].decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
        return {"path": relative, "text": text, "truncated": len(raw) > MAX_TEXT_BYTES}

    def diff(self, profile_id, relative, runner=None):
        self.file(profile_id, relative)
        if runner is None:
            raise ValueError("Git diff требует подключённый защищённый транспорт Codex")
        return runner(str(relative))

    def open_vscode(self, profile_id):
        executable = shutil.which("Code.exe")
        if not executable:
            shim = shutil.which("code")
            if shim:
                candidate = Path(shim).resolve().parent.parent / "Code.exe"
                if candidate.is_file():
                    executable = str(candidate)
        if not executable:
            raise ValueError("VS Code не найден в PATH")
        subprocess.Popen([executable, "--new-window", str(self.root(profile_id))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
