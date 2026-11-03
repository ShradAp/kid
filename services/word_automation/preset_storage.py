"""Preset loading and atomic persistence, separate from Pydantic models."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from .preset_schema import (
        StylePreset, PresetError, PresetNotFoundError, PRESETS_DIRNAME,
        DEFAULT_PRESET_NAME, default_gost_preset, _validate_preset_dict, _log,
    )
except ImportError:  # Standalone module compatibility.
    from preset_schema import (
        StylePreset, PresetError, PresetNotFoundError, PRESETS_DIRNAME,
        DEFAULT_PRESET_NAME, default_gost_preset, _validate_preset_dict, _log,
    )


def presets_dir() -> Path:
    """Каталог пресетов (создаётся при обращении).

    По умолчанию ``<каталог данных пользователя>/presets``; переопределяется переменной
    окружения ``KID_WORD_PRESETS_DIR``.
    """
    override = os.environ.get("KID_WORD_PRESETS_DIR", "").strip().strip("\"'")
    if override:
        base = Path(os.path.expanduser(os.path.expandvars(override)))
    else:
        from app_paths import USER_DATA_DIR

        base = USER_DATA_DIR / PRESETS_DIRNAME
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"word_automation: не удалось создать каталог пресетов {base}: {exc}")
    return base


def safe_preset_name(name: str) -> str:
    """Имя пресета, безопасное для использования как имя файла."""
    text = str(name or "").strip().replace("\\", "/").split("/")[-1]
    text = re.sub(r"[\x00-\x1f<>:\"\\|?*]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip("._")
    if text.lower().endswith(".json"):
        text = text[:-5]
    text = text[:80].strip("._")
    return text or DEFAULT_PRESET_NAME


def preset_path(name: str) -> Path:
    """Полный путь к ``presets/<preset_name>.json``."""
    return presets_dir() / f"{safe_preset_name(name)}.json"


def list_presets() -> list[str]:
    """Имена доступных пресетов (без расширения), отсортированные."""
    try:
        return sorted(
            item.stem for item in presets_dir().glob("*.json") if item.is_file()
        )
    except OSError as exc:
        _log(f"word_automation: не удалось прочитать каталог пресетов: {exc}")
        return []


def save_preset(preset: StylePreset, name: str | None = None) -> str:
    """Сохраняет пресет в ``presets/<name>.json`` и возвращает путь к файлу."""
    if not isinstance(preset, StylePreset):
        raise PresetError(
            f"save_preset ожидает StylePreset, получено: {type(preset).__name__}"
        )
    target_name = safe_preset_name(name or preset.name)
    if target_name != preset.name:
        preset = preset.clone(name=target_name)
    path = preset_path(target_name)
    payload = preset.to_json(indent=2)
    # Атомарная запись: сначала временный файл, затем замена — чтобы пресет не
    # оказался «побитым» при обрыве записи.
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise PresetError(
            f"Не удалось сохранить пресет '{target_name}': {exc}"
        ) from exc
    return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    """Читает JSON пресета с внятными ошибками."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PresetNotFoundError(
            f"Не удалось прочитать файл пресета {path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PresetError(
            f"Файл пресета {path} содержит некорректный JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PresetError(
            f"Пресет {path} должен быть JSON-объектом, "
            f"получено: {type(data).__name__}"
        )
    return data


def ensure_default_preset(*, force: bool = False) -> str:
    """Создаёт дефолтный пресет по ГОСТ, если его ещё нет. Возвращает путь."""
    path = preset_path(DEFAULT_PRESET_NAME)
    if path.exists() and not force:
        return str(path)
    saved = save_preset(default_gost_preset())
    _log(f"word_automation: создан дефолтный пресет ГОСТ -> {saved}")
    return saved


def load_preset(
    source: "StylePreset | str | Path | dict[str, Any] | None" = None,
) -> StylePreset:
    """Загружает пресет из имени, пути, словаря или возвращает переданный объект.

    Поддерживаемые варианты ``source``:
      * ``StylePreset`` — возвращается как есть;
      * ``dict`` — валидируется Pydantic-моделью;
      * ``str``/``Path`` — имя пресета в ``presets/``, путь к ``.json``
        либо inline-JSON (строка, начинающаяся с ``{``);
      * ``None``/пустая строка — дефолтный пресет по ГОСТ.

    Если дефолтный пресет отсутствует на диске — он создаётся автоматически.
    """
    if source is None:
        source = DEFAULT_PRESET_NAME
    if isinstance(source, StylePreset):
        return source
    if isinstance(source, Path):
        if not source.exists():
            raise PresetNotFoundError(f"Файл пресета не найден: {source}")
        return _validate_preset_dict(_load_json(source), origin=str(source))
    if isinstance(source, dict):
        return _validate_preset_dict(source, origin="<dict>")

    if isinstance(source, str):
        text = source.strip() or DEFAULT_PRESET_NAME

        # 1) inline-JSON прямо в строке
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise PresetError(
                    f"Строка пресета похожа на JSON, но не разбирается: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise PresetError(
                    "Inline-пресет должен быть JSON-объектом, "
                    f"получено: {type(data).__name__}"
                )
            return _validate_preset_dict(data, origin="<inline-json>")

        # 2) явный путь к файлу (есть расширение .json либо разделитель пути)
        looks_like_path = (
            text.lower().endswith(".json")
            or os.path.sep in text
            or "/" in text
            or ":" in text
        )
        if looks_like_path:
            candidate = Path(text)
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
            if not candidate.exists():
                raise PresetNotFoundError(f"Файл пресета не найден: {candidate}")
            return _validate_preset_dict(_load_json(candidate), origin=str(candidate))

        # 3) имя пресета в каталоге presets/
        path = preset_path(text)
        if not path.exists():
            if safe_preset_name(text) == DEFAULT_PRESET_NAME:
                ensure_default_preset()
            else:
                available = ", ".join(list_presets()) or "нет"
                raise PresetNotFoundError(
                    f"Пресет '{text}' не найден ({path}). "
                    f"Доступные пресеты: {available}."
                )
        return _validate_preset_dict(_load_json(path), origin=str(path))

    raise PresetError(
        "Неподдерживаемый тип источника пресета: "
        f"{type(source).__name__}. Ожидается StylePreset, str, Path или dict."
    )


def resolve_preset(
    preset: "StylePreset | str | Path | dict[str, Any] | None",
) -> StylePreset:
    """Псевдоним :func:`load_preset` — используется движком и экстрактором."""
    return load_preset(preset)


