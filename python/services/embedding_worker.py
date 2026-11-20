"""
Embedding worker subprocess — isolates PyTorch/sentence-transformers
in a separate process to prevent segfaults from crashing the main server.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any


_MODEL_RELATIVE_PATH = os.environ.get("LOCAL_EMBEDDING_MODEL_PATH", "models/bge-m3")
_DEVICE = "cpu"
_SHUTDOWN_TIMEOUT = 30


def _worker_process(
    request_queue: multiprocessing.Queue,
    response_queue: multiprocessing.Queue,
    configured_model_path: str,
):
    """Loads the embedding model once and processes encode requests."""
    try:
        from sentence_transformers import SentenceTransformer
        model_path = Path(configured_model_path).expanduser()
        if not model_path.is_absolute():
            model_path = Path(__file__).resolve().parents[2] / model_path
        model_path = model_path.resolve()
        if not model_path.is_dir():
            raise FileNotFoundError(f"Local embedding model directory not found: {model_path}")
        model = SentenceTransformer(str(model_path), device=_DEVICE, local_files_only=True)
        warmup = model.encode("warmup", show_progress_bar=False, normalize_embeddings=True)
        if len(warmup) != 1024:
            raise RuntimeError(f"Expected BGE-M3 1024-dimensional vectors, got {len(warmup)}")
        print(f"[embedding_worker] Local model loaded: {model_path}", flush=True)
        response_queue.put(("ready", str(model_path)))
    except Exception as e:
        response_queue.put(("error", str(e)))
        return

    while True:
        try:
            msg = request_queue.get(timeout=1)
        except queue.Empty:
            continue

        if msg is None or msg[0] == "shutdown":
            break

        msg_id, texts = msg  # ("encode", [...])
        try:
            vectors = model.encode(
                texts,
                show_progress_bar=False,
                normalize_embeddings=True,
            ).tolist()
            response_queue.put((msg_id, vectors))
        except Exception as e:
            response_queue.put((msg_id, f"ERROR: {e}"))


class EmbeddingClient:
    """Client that talks to the embedding worker process."""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self._request_queue: multiprocessing.Queue | None = None
        self._response_queue: multiprocessing.Queue | None = None
        self._process: multiprocessing.Process | None = None
        self._counter = 0

    def start(self):
        if self._process is not None:
            return
        ctx = multiprocessing.get_context("spawn")
        self._request_queue = ctx.Queue()
        self._response_queue = ctx.Queue()
        self._process = ctx.Process(
            target=_worker_process,
            args=(self._request_queue, self._response_queue, self.model_path),
            daemon=True,
        )
        self._process.start()
        # Wait for model to load
        try:
            result = self._response_queue.get(timeout=180)
            if result[0] == "error":
                raise RuntimeError(f"Worker failed to load model: {result[1]}")
            if result[0] != "ready":
                raise RuntimeError(f"Unexpected worker startup response: {result[0]}")
        except queue.Empty:
            exit_code = self._process.exitcode if self._process else None
            raise RuntimeError(
                f"Embedding worker timed out during local model startup (exit code: {exit_code})"
            )

    def stop(self):
        if self._process is None:
            return
        try:
            self._request_queue.put(("shutdown", None))
        except Exception:
            pass
        self._process.join(timeout=5)
        if self._process.is_alive():
            self._process.terminate()
        self._process = None
        self._request_queue = None
        self._response_queue = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._process is None or not self._process.is_alive():
            raise RuntimeError("Embedding worker is not running")
        self._counter += 1
        msg_id = f"enc_{self._counter}"
        self._request_queue.put((msg_id, texts))
        try:
            msg_id_resp, result = self._response_queue.get(timeout=300)
            if msg_id_resp != msg_id:
                raise RuntimeError(f"Embedding worker response mismatch: expected {msg_id}, got {msg_id_resp}")
            if isinstance(result, str) and result.startswith("ERROR:"):
                raise RuntimeError(result)
            return result
        except queue.Empty:
            raise RuntimeError("Embedding worker timed out")

    async def aencode(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.encode, texts)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_alive()


# Singleton
_embedding_client: EmbeddingClient | None = None


def get_embedding_client(model_path: str = _MODEL_RELATIVE_PATH) -> EmbeddingClient | None:
    global _embedding_client
    if os.environ.get("DISABLE_LOCAL_EMBEDDINGS") == "1":
        return None
    if _embedding_client is None:
        _embedding_client = EmbeddingClient(model_path)
        try:
            _embedding_client.start()
        except Exception:
            _embedding_client = None
    return _embedding_client
