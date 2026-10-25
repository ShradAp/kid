"""Долговременная память профиля пользователя (только локально, только из диалогов).

Хранилище — LanceDB, таблица ``user_facts`` в ``%APPDATA%/kid/data/lancedb``:
``id``, ``fact``, ``category``, ``vector``, ``created_at``, ``updated_at``.

Эмбеддинги считает FastEmbed (ONNX, multilingual MiniLM) на этой же машине —
внешние API и прокси не участвуют. Модель скачивается один раз в локальный кэш
(``%LOCALAPPDATA%\\kid\\fastembed``) и дальше работает полностью офлайн. Если
FastEmbed/ONNX недоступны, модуль переключается на детерминированный локальный
эмбеддер и продолжает работать (поиск становится лексическим, но данные целы).

Факты попадают сюда исключительно из диалогов: модель вызывает инструмент
``remember_user_fact`` (см. ``api_client.TOOLS_SCHEMA``). Перед каждым запросом
к модели релевантные факты возвращаются в контекст — ``build_memory_context()``.

Модуль намеренно «непадает»: любая ошибка превращается в пустой результат или
в словарь с ``ok=False``, чтобы память профиля никогда не ломала чат.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import uuid
import zlib
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
#                          пути и константы памяти
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    """Корень приложения: как у app_paths.app_dir(), но без жёсткого импорта.

    В собранном exe (PyInstaller onefile) ``__file__`` указывает на временный
    каталог распаковки, поэтому база памяти обязана жить рядом с исполняемым
    файлом — иначе факты терялись бы при каждом запуске.
    """
    try:
        from app_paths import app_dir

        return Path(app_dir())
    except Exception:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent


from app_paths import USER_DATA_DIR

BASE_DIR = _base_dir()
DATA_DIR = USER_DATA_DIR / "data"
LANCEDB_DIR = DATA_DIR / "lancedb"
META_PATH = DATA_DIR / "user_profile_meta.json"

TABLE_NAME = "user_facts"
# Запасное хранилище (если lancedb не установлен в текущем интерпретаторе):
# JSON в каталоге данных пользователя с теми же полями, что и в LanceDB.
JSON_TABLE_FILE = "user_facts.json"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
VECTOR_DIM = 384

FACT_CATEGORIES = (
    "tech_stack",
    "preferences",
    "hardware",
    "rules",
    "personal_context",
)
CATEGORY_LABELS = {
    "tech_stack": "технологии",
    "preferences": "предпочтения",
    "hardware": "железо",
    "rules": "правила",
    "personal_context": "контекст",
}
DEFAULT_CATEGORY = "personal_context"

MAX_FACT_CHARS = 400
MAX_QUERY_CHARS = 600
# Порог «это тот же самый факт» — такая запись обновляется, а не дублируется.
UPDATE_SIMILARITY = 0.90
# Пока фактов мало, в контекст уходят все — порог релевантности не применяется.
MAX_ALWAYS_FACTS = 8
MIN_SCORE = {"fastembed": 0.22, "hash": 0.10}

# Маркеры блока памяти: по ним блок можно извлечь и обновить в системном
# промпте (например, сразу после того, как модель сохранила новый факт).
MEMORY_BLOCK_START = "[ПАМЯТЬ ПРОФИЛЯ ПОЛЬЗОВАТЕЛЯ]"
MEMORY_BLOCK_END = "[/ПАМЯТЬ ПРОФИЛЯ ПОЛЬЗОВАТЕЛЯ]"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_text(value: object, limit: int = MAX_FACT_CHARS) -> str:
    """Схлопывает пробелы и обрезает текст до разумной длины."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip(" ,;:-") + "…"
    return text


def _normalize_category(value: object) -> str:
    raw = str(value if value is not None else "").strip().lower()
    return raw if raw in FACT_CATEGORIES else DEFAULT_CATEGORY


def _model_cache_dir() -> str:
    """Кэш ONNX-модели: вне OneDrive-папки проекта, чтобы не синхронизировать сотни МБ."""
    override = os.environ.get("KID_EMBED_CACHE")
    if override:
        return override
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    else:
        root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return str(Path(root) / "kid" / "fastembed")


def _where_id(fact_id: str) -> str:
    """SQL-условие для LanceDB; одинарные кавычки экранируем на случай мусора."""
    safe = str(fact_id).replace("'", "''")
    return f"id = '{safe}'"


def _install_hint() -> str:
    """Готовая команда установки памяти профиля в ТЕКУЩИЙ интерпретатор.

    Приложение может быть запущено не тем Python, где ставили зависимости
    (автозапуск WEB_UI.pyw использует системный интерпретатор), поэтому подсказка
    всегда содержит конкретный путь к python.exe.
    """
    exe = str(sys.executable or "python")
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"
    return f'"{exe}" -m pip install lancedb fastembed'


def _friendly_error(exc: Exception) -> str:
    """Текст ошибки для лога и для ответа модели — без сырых трейсбеков."""
    if isinstance(exc, ImportError):
        missing = str(getattr(exc, "name", "") or "").strip()
        return (
            f"не установлен пакет {missing or 'lancedb/fastembed'} "
            f"в интерпретаторе {sys.executable}. Установите: {_install_hint()}"
        )
    return f"{type(exc).__name__}: {exc}"


def _cosine(left: list[float], right: list[float]) -> float:
    """Косинусная близость двух векторов (0.0 при вырожденных векторах)."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for value_a, value_b in zip(left, right):
        dot += value_a * value_b
        norm_a += value_a * value_a
        norm_b += value_b * value_b
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


# ---------------------------------------------------------------------------
#                       запасной локальный эмбеддер
# ---------------------------------------------------------------------------


class _HashEmbedder:
    """Детерминированный эмбеддер «мешок слов + основы» без единой зависимости.

    Используется, только если FastEmbed не поднялся (нет onnxruntime, не скачалась
    модель, битый кэш). Даёт устойчивый лексический поиск: полностью локальный,
    одинаковый между запусками процесса и между перезапусками приложения.
    """

    name = "hash"

    def __init__(self, dim: int = VECTOR_DIM) -> None:
        self.dim = int(dim)
        self._token_re = re.compile(r"[0-9a-zа-яё]+")

    def _features(self, text: object) -> list[str]:
        tokens = self._token_re.findall(str(text if text is not None else "").lower())
        features: list[str] = []
        for token in tokens:
            features.append(token)
            if len(token) > 4:
                # основа слова: «python» и «python-разработчик» попадают в один бакет
                features.append(token[:4])
        return features

    def embed_one(self, text: object) -> list[float]:
        vector = [0.0] * self.dim
        for feature in self._features(text):
            # crc32, а не hash(): встроенный hash для str солится между запусками
            digest = zlib.crc32(feature.encode("utf-8"))
            index = digest % self.dim
            vector[index] += 1.0 if (digest >> 16) & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    def embed(self, texts) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]


# ---------------------------------------------------------------------------
#              запасное хранилище фактов (JSON вместо LanceDB)
# ---------------------------------------------------------------------------


class _JsonQuery:
    """Аналог ``table.search(vector).metric(...).limit(...).to_list()``."""

    def __init__(self, rows: list[dict], vector: list[float]) -> None:
        self._rows = rows
        self._vector = [float(value) for value in (vector or [])]
        self._limit = 10

    def metric(self, _name: object) -> "_JsonQuery":
        # всегда косинусная мера — как в основном коде работы с LanceDB
        return self

    def limit(self, count: object) -> "_JsonQuery":
        try:
            self._limit = max(1, int(count))
        except Exception:
            pass
        return self

    def to_list(self) -> list[dict]:
        scored: list[tuple[float, dict]] = []
        for row in self._rows:
            distance = 1.0 - _cosine(self._vector, row.get("vector") or [])
            scored.append((distance, dict(row)))
        scored.sort(key=lambda pair: pair[0])
        found: list[dict] = []
        for distance, row in scored[: self._limit]:
            row["_distance"] = distance
            found.append(row)
        return found


class _JsonArrow:
    """Аналог ``table.to_arrow()``: только ``to_pylist()``, как в ``all_facts``."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict]:
        return [dict(row) for row in self._rows]


class _JsonTable:
    """Запасная таблица фактов в JSON-файле (без единой внешней зависимости).

    Повторяет узкий интерфейс таблицы LanceDB, который использует
    ``UserProfileMemory`` (``count_rows``/``add``/``delete``/``update``/
    ``search``/``to_arrow``), поэтому код записи и поиска остаётся общим.
    Включается автоматически, если ``lancedb``/``pyarrow`` не установлены в
    текущем интерпретаторе: память профиля продолжает работать, а факты
    переезжают в LanceDB, как только он появляется (``_migrate_json_rows``).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------- служебное

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                rows = data.get("rows") if isinstance(data, dict) else data
                if isinstance(rows, list):
                    self._rows = [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            self._rows = []

    def _flush(self) -> None:
        payload = {"schema_version": 1, "table": TABLE_NAME, "rows": self._rows}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _match_ids(where: object) -> set:
        match = re.search(r"id\s*=\s*'(.*)'\s*$", str(where if where is not None else ""), re.DOTALL)
        if not match:
            return set()
        return {match.group(1).replace("''", "'")}

    # --------------------------------------------------- интерфейс «таблицы»

    def count_rows(self) -> int:
        with self._lock:
            return len(self._rows)

    def add(self, rows) -> None:
        with self._lock:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                item["vector"] = [float(value) for value in (item.get("vector") or [])]
                self._rows.append(item)
            self._flush()

    def delete(self, where: object) -> None:
        ids = self._match_ids(where)
        with self._lock:
            self._rows = [row for row in self._rows if str(row.get("id")) not in ids]
            self._flush()

    def update(self, where: object, values: dict) -> None:
        ids = self._match_ids(where)
        with self._lock:
            for row in self._rows:
                if str(row.get("id")) not in ids:
                    continue
                for key, value in (values or {}).items():
                    row[key] = (
                        [float(item) for item in value] if key == "vector" else value
                    )
            self._flush()

    def search(self, vector) -> _JsonQuery:
        with self._lock:
            return _JsonQuery([dict(row) for row in self._rows], list(vector or []))

    def to_arrow(self) -> _JsonArrow:
        with self._lock:
            return _JsonArrow([dict(row) for row in self._rows])

    def reset(self) -> None:
        with self._lock:
            self._rows = []
            self._flush()


# ---------------------------------------------------------------------------
#                               хранилище фактов
# ---------------------------------------------------------------------------


class UserProfileMemory:
    """Локальная база фактов о пользователе: LanceDB + FastEmbed.

    Потокобезопасна (мост — ThreadingHTTPServer), ленива (ничего не грузит до
    первого обращения) и «тихая»: ошибки остаются в ``last_error`` и в логе.
    """

    def __init__(
        self,
        db_dir: str | os.PathLike[str] | None = None,
        model_name: str = EMBED_MODEL,
        log_cb=None,
    ) -> None:
        self.db_dir = Path(db_dir) if db_dir else LANCEDB_DIR
        self.meta_path = (
            self.db_dir.parent / "user_profile_meta.json" if db_dir else META_PATH
        )
        # Запасное хранилище: обычный JSON рядом с data/lancedb. Нужен, чтобы
        # память продолжала работать, если lancedb/pyarrow не установлены в
        # ТЕКУЩЕМ интерпретаторе (приложение автозапускается системным Python).
        self.json_path = self.db_dir.parent / JSON_TABLE_FILE
        self.model_name = model_name
        self._log_cb = log_cb
        self._lock = threading.RLock()
        self._db = None
        self._table = None
        self._embedder = None
        self.backend: str | None = None  # "fastembed" | "hash"
        self.storage: str | None = None  # "lancedb" | "json"
        self.dim = VECTOR_DIM
        self.last_error = ""
        self.ready = False

    # ------------------------------------------------------------ служебное

    def set_log_callback(self, log_cb) -> None:
        with self._lock:
            self._log_cb = log_cb

    def _log(self, message: str) -> None:
        callback = self._log_cb
        if callback is None:
            return
        try:
            callback(message)
        except Exception:
            pass

    def _init_embedder(self) -> None:
        """Поднимает локальные эмбеддинги: FastEmbed, а при сбое — hash-fallback."""
        fastembed_error: Exception | None = None
        # KID_MEMORY_EMBEDDER=hash — принудительный лексический режим (отладка,
        # машины без onnxruntime). Оба режима полностью локальные.
        forced = os.environ.get("KID_MEMORY_EMBEDDER", "").strip().lower()
        if forced != "hash":
            try:
                from fastembed import TextEmbedding

                cache_dir = _model_cache_dir()
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                model = TextEmbedding(model_name=self.model_name, cache_dir=cache_dir)
                # Контрольный прогон: без него «успешная» инициализация может
                # оказаться отложенной загрузкой, которая упадёт уже в чате.
                probe = list(model.embed(["проверка локальной памяти"]))
                dim = len(probe[0]) if probe else VECTOR_DIM
                self._embedder = model
                self.backend = "fastembed"
                self.dim = int(dim)
                self._log(
                    f"[MEMORY] Эмбеддинги: FastEmbed {self.model_name} "
                    f"(dim={self.dim}, кэш {cache_dir})"
                )
                return
            except Exception as exc:  # noqa: BLE001 — память не должна ломать чат
                fastembed_error = exc
        else:
            self.last_error = "KID_MEMORY_EMBEDDER=hash (лексический режим задан вручную)"

        self._embedder = _HashEmbedder(VECTOR_DIM)
        self.backend = "hash"
        self.dim = VECTOR_DIM
        if fastembed_error is not None:
            self.last_error = f"{type(fastembed_error).__name__}: {fastembed_error}"
        self._log(
            "[!] Память профиля работает на локальном лексическом эмбеддере "
            f"({self.last_error or 'без FastEmbed'})"
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self._embedder is None:
            self._init_embedder()
        embedder = self._embedder
        if self.backend == "fastembed":
            return [[float(value) for value in vec] for vec in embedder.embed(texts)]
        return embedder.embed(texts)

    # ---------------------------------------------------------- метаданные

    def _read_meta(self) -> dict:
        try:
            if self.meta_path.exists():
                data = json.loads(self.meta_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _write_meta(self) -> None:
        payload = {
            "table": TABLE_NAME,
            "model": self.model_name,
            "backend": self.backend,
            "storage": self.storage,
            "dim": self.dim,
            "db_dir": str(self.db_dir),
            "updated_at": _now_iso(),
        }
        try:
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            self.meta_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _arrow_schema(self):
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("fact", pa.string()),
                pa.field("category", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self.dim)),
                pa.field("created_at", pa.string()),
                pa.field("updated_at", pa.string()),
            ]
        )

    def _safe_count(self) -> int:
        try:
            return int(self._table.count_rows()) if self._table is not None else 0
        except Exception:
            return 0

    def _embedder_changed(self, meta: dict) -> bool:
        """True, если сохранённые векторы сделаны другим эмбеддером (несовместимы)."""
        return bool(meta) and (
            meta.get("backend") not in (None, self.backend)
            or int(meta.get("dim") or 0) not in (0, self.dim)
        )

    def _open_lancedb_table(self):
        """Открывает/создаёт таблицу LanceDB; None — если lancedb/pyarrow недоступны."""
        try:
            import lancedb

            self.db_dir.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.db_dir))
            try:
                names = list(self._db.table_names())
            except Exception:
                names = []
            exists = TABLE_NAME in names

            meta = self._read_meta()
            if exists and self._embedder_changed(meta):
                # Векторы прошлого эмбеддера несовместимы с текущими — иначе
                # поиск выдавал бы мусор. Пересоздаём таблицу с чистого листа.
                self._log(
                    "[!] Эмбеддер памяти профиля изменился "
                    f"({meta.get('backend')}/{meta.get('dim')} → "
                    f"{self.backend}/{self.dim}) — таблица user_facts пересоздана"
                )
                try:
                    self._db.drop_table(TABLE_NAME)
                except Exception:
                    pass
                exists = False

            table = (
                self._db.open_table(TABLE_NAME)
                if exists
                else self._db.create_table(
                    TABLE_NAME, schema=self._arrow_schema(), exist_ok=True
                )
            )
            self._migrate_json_rows(table)
            return table
        except Exception as exc:  # noqa: BLE001 — память не должна ломать чат
            self._db = None
            self.last_error = _friendly_error(exc)
            self._log(
                f"[!] LanceDB недоступен ({self.last_error}). Память профиля "
                "переключается на локальное JSON-хранилище: факты продолжают "
                "сохраняться и всплывать в диалогах, а после установки lancedb "
                "автоматически переедут обратно в LanceDB."
            )
            return None

    def _open_json_table(self) -> _JsonTable:
        """Запасное хранилище фактов: JSON в ./data, работает без внешних пакетов."""
        table = _JsonTable(self.json_path)
        if self._embedder_changed(self._read_meta()):
            table.reset()
            self._log(
                "[!] Эмбеддер памяти профиля изменился — JSON-хранилище фактов очищено"
            )
        return table

    def _migrate_json_rows(self, table) -> int:
        """Переносит факты из запасного JSON в LanceDB, когда он снова доступен."""
        try:
            if not self.json_path.exists():
                return 0
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            rows = data.get("rows") if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                return 0
            existing = {str(row.get("id")) for row in table.to_arrow().to_pylist()}
            moved = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                fact_id = str(row.get("id") or "")
                vector = row.get("vector") or []
                if not fact_id or fact_id in existing or len(vector) != self.dim:
                    continue
                table.add(
                    [
                        {
                            "id": fact_id,
                            "fact": str(row.get("fact") or ""),
                            "category": _normalize_category(row.get("category")),
                            "vector": [float(value) for value in vector],
                            "created_at": str(row.get("created_at") or _now_iso()),
                            "updated_at": str(row.get("updated_at") or _now_iso()),
                        }
                    ]
                )
                existing.add(fact_id)
                moved += 1
            if moved:
                try:
                    os.replace(
                        self.json_path, self.json_path.with_suffix(".json.migrated")
                    )
                except Exception:
                    pass
                self._log(
                    f"[MEMORY] Перенесено фактов из JSON-хранилища в LanceDB: {moved}"
                )
            return moved
        except Exception:
            return 0

    def _ensure_table(self):
        """Открывает хранилище фактов: LanceDB, а при его отсутствии — JSON."""
        with self._lock:
            if self._table is not None:
                return self._table
            if self._embedder is None:
                self._init_embedder()

            table = self._open_lancedb_table()
            self.storage = "lancedb" if table is not None else "json"
            if table is None:
                table = self._open_json_table()
            self._table = table
            self._write_meta()
            self.ready = True
            self._log(
                f"[MEMORY] Хранилище профиля готово ({self.storage}): "
                f"{self.db_dir if self.storage == 'lancedb' else self.json_path} "
                f"(фактов: {self._safe_count()})"
            )
            return self._table

    # ---------------------------------------------------------- запись факта

    def _find_similar(self, table, vector: list[float], threshold: float) -> dict | None:
        """Ищет почти такой же факт — чтобы обновить его, а не плодить дубли."""
        try:
            if int(table.count_rows()) == 0:
                return None
            rows = table.search(vector).metric("cosine").limit(1).to_list()
        except Exception:
            return None
        if not rows:
            return None
        score = 1.0 - float(rows[0].get("_distance", 1.0))
        if score >= threshold:
            return {"id": rows[0].get("id", ""), "score": score}
        return None

    def add_fact(
        self, fact: object, category: object = DEFAULT_CATEGORY
    ) -> dict:
        """Сохраняет факт из диалога: новый вставляет, почти дубликат — обновляет."""
        text = _normalize_text(fact)
        if not text:
            return {
                "ok": False,
                "status": "error",
                "message": "Пустой факт — сохранять нечего.",
            }
        cat = _normalize_category(category)
        try:
            table = self._ensure_table()
            with self._lock:
                vector = self._embed([text])[0]
                stamp = _now_iso()
                match = self._find_similar(table, vector, UPDATE_SIMILARITY)
                if match and match.get("id"):
                    fact_id = str(match["id"])
                    values = {
                        "fact": text,
                        "category": cat,
                        "vector": vector,
                        "updated_at": stamp,
                    }
                    try:
                        table.update(where=_where_id(fact_id), values=values)
                    except Exception:
                        # update не сработал (например, векторная колонка) —
                        # перезаписываем запись удалением и вставкой.
                        row = {
                            "id": fact_id,
                            "fact": text,
                            "category": cat,
                            "vector": vector,
                            "created_at": stamp,
                            "updated_at": stamp,
                        }
                        table.delete(_where_id(fact_id))
                        table.add([row])
                    return {
                        "ok": True,
                        "status": "updated",
                        "id": fact_id,
                        "fact": text,
                        "category": cat,
                        "created_at": stamp,
                        "updated_at": stamp,
                    }

                fact_id = f"fact_{uuid.uuid4().hex[:12]}"
                table.add(
                    [
                        {
                            "id": fact_id,
                            "fact": text,
                            "category": cat,
                            "vector": vector,
                            "created_at": stamp,
                            "updated_at": stamp,
                        }
                    ]
                )
                return {
                    "ok": True,
                    "status": "created",
                    "id": fact_id,
                    "fact": text,
                    "category": cat,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
        except Exception as exc:  # noqa: BLE001
            self.last_error = _friendly_error(exc)
            self._log(f"[!] Не удалось сохранить факт профиля: {self.last_error}")
            return {
                "ok": False,
                "status": "error",
                "message": self.last_error,
            }

    # ----------------------------------------------------------- поиск фактов

    def search(self, query: object, limit: int = 6, min_score: float | None = None) -> list[dict]:
        """Векторный поиск по фактам; пустой список вместо исключения при сбое."""
        text = _normalize_text(query, MAX_QUERY_CHARS)
        if not text:
            return []
        try:
            table = self._ensure_table()
            with self._lock:
                total = self._safe_count()
                if total == 0:
                    return []
                threshold = (
                    MIN_SCORE.get(self.backend or "fastembed", 0.30)
                    if min_score is None
                    else float(min_score)
                )
                # Маленький профиль отдаём целиком: несколько фактов полезнее,
                # чем риск отсечь нужный по формальному порогу.
                small_profile = total <= MAX_ALWAYS_FACTS
                fetch = min(total, max(int(limit), total if small_profile else 1))
                rows = table.search(self._embed([text])[0]).metric("cosine").limit(fetch).to_list()
        except Exception as exc:  # noqa: BLE001
            self.last_error = _friendly_error(exc)
            self._log(f"[!] Поиск по памяти профиля не удался: {self.last_error}")
            return []

        found: list[dict] = []
        for row in rows:
            score = 1.0 - float(row.get("_distance", 1.0))
            if not small_profile and score < threshold:
                continue
            found.append(
                {
                    "id": row.get("id", ""),
                    "fact": row.get("fact", ""),
                    "category": row.get("category", DEFAULT_CATEGORY),
                    "score": round(score, 4),
                    "created_at": row.get("created_at", ""),
                    "updated_at": row.get("updated_at", ""),
                }
            )
        return found

    def count(self) -> int:
        try:
            self._ensure_table()
            return self._safe_count()
        except Exception:
            return 0

    def clear(self) -> bool:
        """Полная очистка памяти профиля (хранилище пересоздаётся пустым)."""
        try:
            with self._lock:
                table = self._ensure_table()
                if isinstance(table, _JsonTable):
                    table.reset()
                    return True
                if self._db is None:
                    return False
                self._db.drop_table(TABLE_NAME)
                self._table = self._db.create_table(
                    TABLE_NAME, schema=self._arrow_schema(), exist_ok=True
                )
                del table
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = _friendly_error(exc)
            return False

    # -------------------------------------------------- чтение и правка фактов

    def all_facts(self) -> list[dict]:
        """Все факты профиля (для чтения и редактирования моделью).

        Возвращает список ``{id, fact, category, created_at, updated_at}``;
        при любой ошибке — пустой список (память не должна ломать чат).
        """
        try:
            table = self._ensure_table()
            rows = table.to_arrow().to_pylist()
        except Exception as exc:  # noqa: BLE001
            self.last_error = _friendly_error(exc)
            self._log(f"[!] Не удалось прочитать факты профиля: {self.last_error}")
            return []
        facts: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            facts.append(
                {
                    "id": str(row.get("id", "")),
                    "fact": str(row.get("fact", "")),
                    "category": str(row.get("category", DEFAULT_CATEGORY)),
                    "created_at": str(row.get("created_at", "")),
                    "updated_at": str(row.get("updated_at", "")),
                }
            )
        return facts

    def delete_fact(self, fact_id: object) -> dict:
        """Удаляет один факт по ``id``; не найдя запись — сообщает об этом."""
        fid = str(fact_id if fact_id is not None else "").strip()
        if not fid:
            return {"ok": False, "status": "error", "message": "Пустой id факта — нечего удалять."}
        try:
            table = self._ensure_table()
            with self._lock:
                before = self._safe_count()
                table.delete(_where_id(fid))
                after = self._safe_count()
        except Exception as exc:  # noqa: BLE001
            self.last_error = _friendly_error(exc)
            self._log(f"[!] Не удалось удалить факт профиля: {self.last_error}")
            return {"ok": False, "status": "error", "message": self.last_error}
        if after < before:
            self._log(f"[MEMORY] Факт удалён из памяти профиля: {fid}")
            return {"ok": True, "status": "deleted", "id": fid,
                    "message": f"Факт {fid} удалён из памяти профиля."}
        return {"ok": False, "status": "not_found",
                "message": f"Факт с id {fid} не найден — удалять нечего."}

    def update_fact(
        self, fact_id: object, fact: object, category: object = DEFAULT_CATEGORY
    ) -> dict:
        """Перезаписывает существующий факт по ``id`` (текст + категория).

        Реализация через удаление и вставку с тем же id — надёжно и для LanceDB,
        и для JSON-хранилища (update векторной колонки в LanceDB может падать).
        """
        fid = str(fact_id if fact_id is not None else "").strip()
        text = _normalize_text(fact)
        cat = _normalize_category(category)
        if not fid:
            return {"ok": False, "status": "error", "message": "Пустой id факта — нечего обновлять."}
        if not text:
            return {"ok": False, "status": "error", "message": "Пустой fact — обновлять нечем."}
        try:
            table = self._ensure_table()
            with self._lock:
                rows = [row for row in (table.to_arrow().to_pylist() or []) if isinstance(row, dict)]
                original = next((row for row in rows if str(row.get("id")) == fid), None)
                if original is None:
                    return {"ok": False, "status": "not_found",
                            "message": f"Факт с id {fid} не найден — обновлять нечего."}
                vector = self._embed([text])[0]
                stamp = _now_iso()
                created_at = str(original.get("created_at") or stamp)
                table.delete(_where_id(fid))
                table.add(
                    [
                        {
                            "id": fid,
                            "fact": text,
                            "category": cat,
                            "vector": vector,
                            "created_at": created_at,
                            "updated_at": stamp,
                        }
                    ]
                )
        except Exception as exc:  # noqa: BLE001
            self.last_error = _friendly_error(exc)
            self._log(f"[!] Не удалось обновить факт профиля: {self.last_error}")
            return {"ok": False, "status": "error", "message": self.last_error}
        self._log(f"[MEMORY] Факт обновлён в памяти профиля: {fid}")
        return {"ok": True, "status": "updated", "id": fid, "fact": text, "category": cat,
                "message": (f"Факт обновлён: «{text}» "
                            f"(категория: {CATEGORY_LABELS.get(cat, 'контекст')}).")}


# ---------------------------------------------------------------------------
#              публичный API модуля (singleton + готовые хелперы)
# ---------------------------------------------------------------------------

_store: UserProfileMemory | None = None
_store_lock = threading.Lock()


def get_store() -> UserProfileMemory:
    """Единственный экземпляр хранилища на процесс (ленивая инициализация)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = UserProfileMemory()
        return _store


def set_log_callback(log_cb) -> None:
    """Пробрасывает события памяти в общий лог приложения."""
    try:
        get_store().set_log_callback(log_cb)
    except Exception:
        pass


def warmup() -> str:
    """Прогрев в фоне: грузит модель и хранилище до первого сообщения пользователя.

    Возвращает имя активного бэкенда эмбеддингов («fastembed» или «hash»).
    """
    try:
        store = get_store()
        store._ensure_table()
        if store.storage != "lancedb":
            # Память работает, но не на LanceDB: говорим об этом прямо и сразу
            # подсказываем, как вернуть полноценный режим.
            store._log(
                "[!] Память профиля в запасном режиме (JSON-хранилище): "
                f"{store.last_error or 'LanceDB недоступен'}. Для полноценного "
                f"режима выполните: {_install_hint()}"
            )
        return store.backend or "unavailable"
    except Exception:
        return "unavailable"


def remember_user_fact(fact: object, category: object = DEFAULT_CATEGORY) -> dict:
    """Обработчик инструмента ``remember_user_fact`` из диалога.

    Возвращает ``{"ok", "status", "message", ...}``; ``message`` — готовый текст
    ответа инструмента для модели.
    """
    result = get_store().add_fact(fact, category)
    status = result.get("status", "error")
    text = result.get("fact", "")
    label = CATEGORY_LABELS.get(result.get("category", DEFAULT_CATEGORY), "контекст")
    if status == "created":
        result["message"] = (
            f"Факт сохранён в долговременную память профиля: «{text}» "
            f"(категория: {label}). Он будет автоматически учитываться в "
            "следующих диалогах — повторно не сохраняй его."
        )
    elif status == "updated":
        result["message"] = (
            f"Такой факт уже был в памяти профиля — запись обновлена: «{text}» "
            f"(категория: {label})."
        )
    else:
        result["message"] = (
            "Не удалось сохранить факт в память профиля "
            f"({result.get('message') or 'локальное хранилище недоступно'}). "
            "Продолжай отвечать как обычно и не повторяй вызов."
        )
    return result


def manage_memory(
    action: object,
    fact_id: object = None,
    fact: object = None,
    category: object = DEFAULT_CATEGORY,
) -> dict:
    """Единый обработчик инструмента ``manage_memory`` (list/update/delete/clear).

    Даёт модели читать и менять долговременную память профиля ТОЛЬКО по явному
    запросу пользователя. Возвращает ``{"ok", "status", "message", ...}``.
    """
    act = str(action if action is not None else "").strip().lower()
    store = get_store()

    if act == "list":
        try:
            facts = store.all_facts()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "error",
                    "message": f"Не удалось прочитать память профиля: {exc}"}
        if not facts:
            return {"ok": True, "status": "ok", "facts": [],
                    "message": "Память профиля пуста — долговременных фактов нет."}
        lines = [
            f"{item['id']} | ({CATEGORY_LABELS.get(item.get('category', DEFAULT_CATEGORY), 'контекст')}) {item['fact']}"
            for item in facts
        ]
        return {"ok": True, "status": "ok", "facts": facts,
                "message": "Факты памяти профиля (id | категория | факт):\n" + "\n".join(lines)}

    if act == "update":
        return store.update_fact(fact_id, fact, category)

    if act == "delete":
        return store.delete_fact(fact_id)

    if act == "clear":
        try:
            ok = bool(store.clear())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "error",
                    "message": f"Не удалось очистить память профиля: {exc}"}
        if ok:
            return {"ok": True, "status": "cleared",
                    "message": "Память профиля полностью очищена."}
        return {"ok": False, "status": "error",
                "message": store.last_error or "Не удалось очистить память профиля."}

    return {"ok": False, "status": "error",
            "message": f"Неизвестное действие manage_memory: {action!r}. "
                       "Допустимы list, update, delete, clear."}


def retrieve_relevant_facts(query: object, limit: int = 6) -> list[dict]:
    """Факты профиля, релевантные сообщению пользователя (локальный поиск)."""
    try:
        return get_store().search(query, limit=limit)
    except Exception:
        return []


def format_facts_block(facts: list[dict]) -> str:
    """Блок памяти для системного промпта (пустая строка, если фактов нет)."""
    usable = [item for item in (facts or []) if str(item.get("fact", "")).strip()]
    if not usable:
        return ""
    lines = [
        MEMORY_BLOCK_START,
        "Долговременные факты о пользователе, извлечённые из прошлых диалогов:",
    ]
    for item in usable:
        label = CATEGORY_LABELS.get(item.get("category", DEFAULT_CATEGORY), "контекст")
        lines.append(f"- ({label}) {str(item['fact']).strip()}")
    lines.append(
        "Используй эти факты естественно, не переспрашивай уже известное и не "
        "объясняй пользователю механизм памяти. Если факт противоречит текущему "
        "сообщению пользователя — приоритет у текущего сообщения."
    )
    lines.append(MEMORY_BLOCK_END)
    return "\n".join(lines)


def build_memory_context(query: object, limit: int = 6) -> str:
    """Готовый блок памяти для вставки в системный промпт перед вызовом модели."""
    try:
        facts = retrieve_relevant_facts(query, limit=limit)
        block = format_facts_block(facts)
    except Exception:
        return ""
    return ("\n\n" + block) if block else ""


def embed_texts(texts: object) -> list[list[float]]:
    """Векторизует тексты тем же локальным эмбеддером, что и память профиля.

    Нужна эмбеддинг-роутеру тулов (tool_gating.select_tools_by_embedding), чтобы
    не грузить вторую модель FastEmbed: эмбеддер переиспользуется из единственного
    хранилища. Возвращает список векторов той же размерности, что и база фактов.
    """
    store = get_store()
    return store._embed([str(t) for t in (texts or [])])


def strip_memory_block(system_text: object) -> str:
    """Убирает старый блок памяти из системного промпта (перед его обновлением)."""
    text = str(system_text if system_text is not None else "")
    if MEMORY_BLOCK_START not in text:
        return text
    start = text.index(MEMORY_BLOCK_START)
    end = text.find(MEMORY_BLOCK_END, start)
    if end == -1:
        return text[:start].rstrip()
    cleaned = text[:start] + text[end + len(MEMORY_BLOCK_END):]
    return cleaned.rstrip()


def stats() -> dict:
    """Сводка для логов/отладки: где база, какой эмбеддер, сколько фактов."""
    store = get_store()
    return {
        "db_dir": str(store.db_dir),
        "table": TABLE_NAME,
        "backend": store.backend,
        "storage": store.storage,
        "model": store.model_name,
        "dim": store.dim,
        "facts": store.count(),
        "python": str(sys.executable),
        "last_error": store.last_error,
    }


def status() -> dict:
    """Состояние памяти профиля для диагностики (исключений не бросает).

    ``degraded`` — память работает, но не на LanceDB (пакет не установлен в
    ТЕКУЩЕМ интерпретаторе); ``install_hint`` — готовая команда установки.
    """
    store = get_store()
    try:
        facts = store.count()
    except Exception:
        facts = 0
    storage = store.storage or "unavailable"
    return {
        "available": bool(store.ready),
        "degraded": storage != "lancedb",
        "storage": storage,
        "backend": store.backend or "unavailable",
        "model": store.model_name,
        "dim": store.dim,
        "facts": facts,
        "db_dir": str(store.db_dir),
        "json_path": str(store.json_path),
        "python": str(sys.executable),
        "last_error": store.last_error,
        "install_hint": _install_hint(),
    }


def install_hint() -> str:
    """Команда установки lancedb/fastembed в интерпретатор, запустивший приложение."""
    return _install_hint()
