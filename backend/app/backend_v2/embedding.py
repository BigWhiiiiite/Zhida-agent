"""Local, revision-pinned BGE embeddings; this module never downloads a model."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Iterable, Protocol

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
# Deployment pin, not a mutable cache refs/main alias. Changing the embedding
# space requires an explicit index migration and acceptance run.
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
DIMENSION = 512
MAX_INPUT_TOKENS = 480
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "embedding_models"


class EmbeddingUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    @property
    def profile(self) -> dict: ...
    def token_count(self, text: str) -> int: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def normalized_vector(values: Iterable[float]) -> list[float]:
    try:
        vector = [float(value) for value in values]
    except (ValueError, TypeError, OverflowError):
        # Malformed cache/model values must not leak into logs via float errors.
        raise ValueError("Embedding must contain 512 finite numeric values") from None
    if len(vector) != DIMENSION or not all(math.isfinite(v) for v in vector):
        raise ValueError("Embedding must contain 512 finite values")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("Zero or invalid embedding cannot be indexed")
    return [value / norm for value in vector]


def vector_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in normalized_vector(values)) + "]"


class LocalBGEEmbedder:
    """Use the installed FastEmbed ONNX snapshot without network access.

    Snapshot identity, tokenizer/query conventions and FastEmbed revision are
    part of the profile. A different profile builds a different generation.
    """
    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE, *, threads: int = 1):
        self.cache_dir = Path(cache_dir)
        self.threads = threads
        self._model = None
        self._tokenizer = None
        self._snapshot = None
        self._profile = None
        self._initialization_lock = RLock()

    @property
    def profile(self) -> dict:
        with self._initialization_lock:
            if self._profile is None:
                root = self.cache_dir / "models--Qdrant--bge-small-zh-v1.5"
                snapshot = root / "snapshots" / MODEL_REVISION
                if not snapshot.is_dir():
                    raise EmbeddingUnavailable("本地固定版本 BGE 模型未预装；系统不会自动下载或切换版本")
                from importlib.metadata import version
                configuration = {"normalization": "unit", "query_prefix": QUERY_PREFIX,
                                 "max_input_tokens": MAX_INPUT_TOKENS,
                                 "fastembed_version": version("fastembed"), "pooling": "cls"}
                fingerprint = hashlib.sha256(json.dumps(
                    [MODEL_NAME, MODEL_REVISION, configuration], sort_keys=True).encode()).hexdigest()[:24]
                self._profile = {"id": "bge512-" + fingerprint, "model_name": MODEL_NAME,
                                 "model_revision": MODEL_REVISION, "dimension": DIMENSION,
                                 "metric": "cosine", "configuration": configuration}
                self._snapshot = snapshot
            return deepcopy(self._profile)

    def _load(self):
        with self._initialization_lock:
            self.profile
            if self._model is None:
                try:
                    import onnxruntime
                    onnxruntime.disable_telemetry_events()
                    from fastembed import TextEmbedding
                    from tokenizers import Tokenizer
                    model = TextEmbedding(
                        model_name=MODEL_NAME, cache_dir=str(self.cache_dir),
                        specific_model_path=str(self._snapshot), local_files_only=True,
                        threads=self.threads, providers=["CPUExecutionProvider"],
                    )
                    # Publish the pair only after complete initialization. The
                    # API and workers may concurrently make their first call.
                    tokenizer = Tokenizer.from_str(model.model.tokenizer.to_str())
                    tokenizer.no_truncation()
                    tokenizer.no_padding()
                    self._tokenizer, self._model = tokenizer, model
                except Exception:
                    raise EmbeddingUnavailable("本地 BGE 模型加载失败；未尝试联网下载") from None
            return self._model

    def token_count(self, text: str) -> int:
        self._load()
        return len(self._tokenizer.encode(text).ids)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        if any(self.token_count(value) > MAX_INPUT_TOKENS for value in texts):
            raise ValueError("Embedding input exceeds tokenizer limit; split before indexing")
        return [normalized_vector(vector) for vector in model.embed(texts, batch_size=16)]

    def embed_query(self, text: str) -> list[float]:
        value = QUERY_PREFIX + text
        # query prefix is applied exactly once, identically to the profile.
        return self.embed_documents([value])[0]
