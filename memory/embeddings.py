"""Deterministic fallback vectors and lazy local FastEmbed model loading."""

import math
import os
import re
import zlib
from pathlib import Path

VECTOR_DIM = 384


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


def load_fastembed(model_name):
    from fastembed import TextEmbedding

    cache_dir = _model_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
    # Контрольный прогон: без него «успешная» инициализация может
    # оказаться отложенной загрузкой, которая упадёт уже в чате.
    probe = list(model.embed(["проверка локальной памяти"]))
    dim = len(probe[0]) if probe else VECTOR_DIM
    return model, dim, cache_dir
