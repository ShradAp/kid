"""Перенос личных данных без перезаписи; по умолчанию только сверка."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

# Только данные kid. Соседние приложения и исходники не затрагиваются.
DATA_NAMES = (
    ".env", "notes.json", "retrieval_metrics.json", "data", "history",
    "generated_files", "presets", ".backups", "frontend.log", "error.log",
    "documents", "notes",
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def migrate(source: Path, destination: Path, *, apply: bool = False) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Source and destination must be separate directories")
    entries = []
    directories = []
    for name in DATA_NAMES:
        root = source / name
        if not root.exists():
            continue
        if root.is_symlink() or root.is_junction():
            raise ValueError(f"Linked source is not supported: {name}")
        paths = [root]
        if root.is_dir():
            for current, dirs, files in os.walk(root, followlinks=False):
                for child in dirs + files:
                    path = Path(current) / child
                    if path.is_symlink() or path.is_junction():
                        raise ValueError("Linked user data is not supported")
                directories.append(Path(current))
                paths.extend(Path(current) / filename for filename in files)
        for path in paths:
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            target = destination / relative
            if not target.resolve().is_relative_to(destination):
                raise ValueError("Destination points outside user data directory")
            checksum = digest(path)
            if target.exists() and (not target.is_file() or digest(target) != checksum):
                raise FileExistsError(f"Different destination already exists: {relative}")
            entries.append((path, target, checksum))

    report = {
        "source": str(source), "destination": str(destination), "apply": apply,
        "files": [{"relative_path": str(p.relative_to(source)), "sha256": h,
                   "already_present": t.exists()} for p, t, h in entries],
    }
    if not apply:
        return report

    destination.mkdir(parents=True, exist_ok=True)
    # Сначала копируем всё и сверяем; исходники удаляются только после проверки.
    for path, target, checksum in entries:
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as src, target.open("xb") as dst:
            while block := src.read(1024 * 1024):
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        if digest(target) != checksum:
            raise OSError("Copied data checksum mismatch; source files preserved")
    for path, target, checksum in entries:
        if digest(path) != checksum or digest(target) != checksum:
            raise OSError("Data changed during migration; source files preserved")

    report_dir = destination / "migration_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for path, target, checksum in entries:
        # Повторная проверка непосредственно перед удалением каждого исходника.
        if digest(path) != checksum or digest(target) != checksum:
            raise OSError("Data changed before removal; remaining sources preserved")
        path.unlink()
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        path.rmdir()  # Только пустые каталоги; новые файлы не удаляются.
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = migrate(args.source, args.destination, apply=args.apply)
    print(json.dumps({"apply": args.apply, "files": len(result["files"]),
                      "already_present": sum(f["already_present"] for f in result["files"])}, indent=2))


if __name__ == "__main__":
    main()
