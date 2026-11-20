"""
向量存储服务 — 支持 ChromaDB / PGVector 双后端

职责:
  1. 文档块向量化 (Embedding)
  2. 向量存储 & 检索
  3. 按 doc_id 删除（支持增量更新）
"""

from __future__ import annotations

from typing import Any

from langchain_openai import OpenAIEmbeddings

from agents.doc_parser_agent import DocumentChunk
from config import settings


class _SubprocessEmbeddings:
    """Embedding wrapper that delegates to a separate subprocess to avoid
    PyTorch segfaults from crashing the main server process."""

    def __init__(self):
        from services.embedding_worker import get_embedding_client
        self._client = get_embedding_client(settings.local_embedding_model_path)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            return [[0.0]] * len(texts)
        return self._client.encode(texts)

    def embed_query(self, text: str) -> list[float]:
        if self._client is None:
            return [0.0]
        return self._client.encode([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            return [[0.0]] * len(texts)
        return await self._client.aencode(texts)

    async def aembed_query(self, text: str) -> list[float]:
        if self._client is None:
            return [0.0]
        result = await self._client.aencode([text])
        return result[0]


def _create_embeddings():
    """根据配置创建 Embedding 实例。"""
    if settings.disable_local_embeddings:
        return None
    if "deepseek" in settings.openai_base_url:
        return _SubprocessEmbeddings()
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )


class VectorStoreService:
    """向量库统一接口，底层可切换 ChromaDB / PGVector"""

    # BGE-M3 emits 1024-dimensional vectors. Keep the old collection intact so
    # existing vectors/data are never silently deleted or dimension-conflicted.
    COLLECTION_NAME = "knowledge_chunks_bge_m3"

    def __init__(self) -> None:
        self._embeddings: Any = None
        self._store: Any = None
        self._backend = settings.vector_store_type
        from concurrent.futures import ThreadPoolExecutor
        self._executor = ThreadPoolExecutor(max_workers=2)

    async def _run_sync(self, fn, *args, **kwargs):
        """Run chromadb operations in thread pool to avoid async segfaults."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    @property
    def embeddings(self):
        if self._embeddings is None:
            # Set DISABLE_LOCAL_EMBEDDINGS=1 to force LLM-only mode.
            if settings.disable_local_embeddings:
                return None
            try:
                self._embeddings = _create_embeddings()
            except Exception:
                self._embeddings = None
        return self._embeddings

    @property
    def embeddings_available(self) -> bool:
        if settings.disable_local_embeddings:
            return False
        if self._embeddings is not None:
            if isinstance(self._embeddings, _SubprocessEmbeddings):
                return self._embeddings._client is not None
            return True
        # Try loading; if it fails, stay disabled
        try:
            return self.embeddings is not None
        except Exception:
            return False

    # ── initialization ───────────────────────────────────────

    async def init(self) -> None:
        if self._backend == "chroma":
            await self._init_chroma()
        else:
            await self._init_pgvector()

    async def _init_chroma(self) -> None:
        def _init():
            import chromadb
            client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
            return client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        self._store = await self._run_sync(_init)

    async def _init_pgvector(self) -> None:
        from langchain_community.vectorstores import PGVector
        self._store = PGVector(
            connection_string=settings.pgvector_dsn,
            collection_name=self.COLLECTION_NAME,
            embedding_function=self.embeddings,
        )

    # ── CRUD ─────────────────────────────────────────────────

    async def add_chunks(self, chunks: list[DocumentChunk]) -> int:
        """向量化并存储文档块到项目隔离的 ChromaDB 服务。"""
        if not chunks or not self.embeddings_available:
            return 0
        vectors = await self.embeddings.aembed_documents([chunk.content for chunk in chunks])
        ids = [chunk.chunk_id for chunk in chunks]
        metadatas = []
        for chunk in chunks:
            metadata = {
                "doc_id": chunk.doc_id,
                "chunk_index": chunk.chunk_index,
                "doc_type": chunk.doc_type.value,
                **chunk.metadata,
            }
            metadatas.append({k: v for k, v in metadata.items() if isinstance(v, (str, int, float, bool))})
        await self._run_sync(
            self._store.upsert,
            ids=ids,
            embeddings=vectors,
            documents=[chunk.content for chunk in chunks],
            metadatas=metadatas,
        )
        return len(chunks)

    async def search(self, query: str, top_k: int = 5) -> list[tuple[dict, float]]:
        """语义搜索并返回 Chroma cosine distance。"""
        if not self.embeddings_available:
            return []
        if self._backend == "chroma":
            vector = await self.embeddings.aembed_query(query)
            result = await self._run_sync(
                self._store.query,
                query_embeddings=[vector],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
            rows = []
            for i, content in enumerate((result.get("documents") or [[]])[0]):
                metadata = (result.get("metadatas") or [[]])[0][i] or {}
                distance = (result.get("distances") or [[]])[0][i]
                rows.append(({
                    "content": content,
                    "source": metadata.get("source", ""),
                    "metadata": metadata,
                }, float(distance)))
            return rows
        results = await self._store.asimilarity_search_with_score(query, k=top_k)
        return [
            ({"content": doc.page_content, "source": doc.metadata.get("source", ""), "metadata": doc.metadata}, score)
            for doc, score in results
        ]

    async def delete_by_doc_id(self, doc_id: str) -> int:
        """按 doc_id 删除所有相关向量"""
        if self._backend == "chroma":
            existing = await self._run_sync(self._store.get, where={"doc_id": doc_id}, include=[])
            ids = existing.get("ids", [])
            if ids:
                await self._run_sync(self._store.delete, ids=ids)
            return len(ids)
        return 0

    async def get_stats(self) -> dict:
        """获取向量库实际统计信息。"""
        if self._backend == "chroma":
            if self._store is None:
                return {"backend": "chroma", "total_vectors": 0, "collection": self.COLLECTION_NAME}
            count = await self._run_sync(self._store.count)
            return {"backend": "chroma", "total_vectors": count, "collection": self.COLLECTION_NAME}
        return {"backend": "pgvector", "collection": self.COLLECTION_NAME}
