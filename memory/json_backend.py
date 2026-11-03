"""Dependency-free JSON fact table and cosine-query adapter."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from pathlib import Path

TABLE_NAME = "user_facts"


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


