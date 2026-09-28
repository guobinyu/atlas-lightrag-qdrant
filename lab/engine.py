"""One asyncio loop owns LightRAG, embedding, and the Qdrant client."""
import asyncio
from concurrent.futures import Future
import importlib.metadata
import json
import os
from pathlib import Path
import re
import threading
import time

import numpy as np
from qdrant_client import QdrantClient, models
from lightrag import LightRAG, QueryParam
from lightrag.kg.qdrant_impl import QdrantVectorDBStorage, workspace_filter_condition
from lightrag.utils import EmbeddingFunc

from lab.config import ROOT, Settings
from lab.embedding import Embedding
from lab.projection import Projection
from lab.trace import Trace, TracedClient, point_id


async def no_generation(*args, **kwargs):
    raise RuntimeError("本应用只执行 naive 文本检索，不配置生成或实体抽取模型。")


def read_chunks(paths):
    chunks = []
    for path in paths:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        sections = re.split(r"\n(?=## )", text)
        for section in sections:
            # Small Chinese-friendly chunks, with overlap for unusually long sections.
            for start in range(0, len(section), 380):
                content = section[start:start + 460].strip()
                if content:
                    chunks.append({"content": content, "source_id": f"{path.name}:{len(chunks)}", "file_path": path.name, "chunk_order_index": len(chunks)})
                if start + 460 >= len(section):
                    break
    return chunks


def safe_error(exc, settings):
    if isinstance(exc, (ValueError, FileNotFoundError)):
        message = str(exc)
    else:
        message = f"{type(exc).__name__}：执行失败。请检查服务是否可达、模型配置及本机终端日志。"
    for secret in (settings.embedding_api_key, settings.qdrant_api_key):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:1000]


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings.validate()
        self.active_trace = None
        self.client = None
        self.rag = None
        from lab.workspace import Workspace
        self.workbench = Workspace(self)
        self.gate = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="retrieval-worker", daemon=True)
        self.thread.start()
        self.ready = asyncio.run_coroutine_threadsafe(self._initialize(), self.loop)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _initialize(self):
        cfg = self.settings
        if importlib.metadata.version("lightrag-hku") != "1.5.7":
            raise RuntimeError("此适配器针对 lightrag-hku 1.5.7 验证，请使用锁定的依赖。")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        identity_path = cfg.rag_dir / cfg.workspace / "retrieval_lab_embedding.json"
        if identity_path.exists():
            existing = json.loads(identity_path.read_text(encoding="utf-8"))
            if existing != cfg.embedding_identity():
                raise ValueError("当前 Embedding 与建库配置不一致。请恢复原配置，或为新模型使用新的 workspace 和数据目录。")
        self.embedder = await asyncio.to_thread(Embedding, cfg, lambda: self.active_trace)

        async def embed(texts, context="document", **kwargs):
            self.workbench.emit("embedding.started", {"model": cfg.embedding_model, "text_count": len(texts), "context": context})
            started = time.perf_counter()
            values = await self.embedder(texts, context=context, **kwargs)
            self.workbench.emit("embedding.completed", {"model": cfg.embedding_model, "text_count": len(texts), "dimension": cfg.embedding_dim, "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
            return values

        async def extract_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
            return await self.workbench.llm(prompt, system_prompt, history_messages, **kwargs)

        # Upstream checks this variable at construction. The actual client below
        # is injected per adapter before initialize(), which supports pre-set clients.
        os.environ.setdefault("QDRANT_URL", cfg.qdrant_url)
        self.rag = LightRAG(
            working_dir=str(cfg.rag_dir), workspace=cfg.workspace,
            vector_storage="QdrantVectorDBStorage",
            llm_model_func=extract_llm,
            chunk_token_size=380, chunk_overlap_token_size=60,
            llm_model_max_async=1, max_parallel_insert=1,
            entity_extract_max_gleaning=0,
            default_llm_timeout=600,
            addon_params={"language": "Chinese"},
            embedding_func=EmbeddingFunc(
                embedding_dim=cfg.embedding_dim, max_token_size=512,
                model_name=cfg.embedding_model, func=embed, supports_asymmetric=True,
            ),
            embedding_batch_num=16, embedding_func_max_async=1,
            default_embedding_timeout=int(cfg.timeout),
            vector_db_storage_cls_kwargs={"cosine_better_than_threshold": 0.2},
            enable_llm_cache=False, enable_llm_cache_for_entity_extract=False,
        )
        self.client = (QdrantClient(path=str(cfg.data_dir / "qdrant")) if cfg.qdrant_mode == "local" else
                       QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, timeout=cfg.timeout))
        try:
            adapters = [self.rag.chunks_vdb, self.rag.entities_vdb, self.rag.relationships_vdb]
            for adapter in adapters:
                if type(adapter) is not QdrantVectorDBStorage or not hasattr(adapter, "_client"):
                    raise RuntimeError("LightRAG Qdrant 适配器结构发生变化。")
                if adapter.effective_workspace != cfg.workspace:
                    raise ValueError("实际 Qdrant workspace 与配置不一致。")
                if cfg.qdrant_mode == "server":
                    # Upstream setup can migrate or clean up legacy collections,
                    # including when a new-model collection already exists.
                    legacy = [f"lightrag_vdb_{adapter.namespace}", f"{cfg.workspace}_{adapter.namespace}", adapter.namespace]
                    if any(name != adapter.final_namespace and self.client.collection_exists(name) for name in legacy):
                        raise ValueError("检测到旧版集合名称。请先通过 LightRAG 官方迁移流程准备当前版本的工作区。")
                if self.client.collection_exists(adapter.final_namespace):
                    vector_config = self.client.get_collection(adapter.final_namespace).config.params.vectors
                    if not isinstance(vector_config, models.VectorParams) or vector_config.size != cfg.embedding_dim or vector_config.distance != models.Distance.COSINE:
                        raise ValueError("现有集合必须使用单个 dense vector、相同维度及 Cosine 度量。")
                adapter._client = TracedClient(self.client, lambda: self.active_trace, self.workbench.emit)
            self.traced_client = self.rag.chunks_vdb._client
            await self.rag.initialize_storages()
            self.collection = self.rag.chunks_vdb.final_namespace
            self.workspace_filter = models.Filter(must=[workspace_filter_condition(cfg.workspace)])
            existing_count = self.client.count(self.collection, count_filter=self.workspace_filter, exact=True).count
            if cfg.seed_demo and existing_count == 0:
                paths = sorted((ROOT / "examples").glob("*.md"))
                # Keep full demo texts available for a later native graph build.
                # Their preloaded chunks do not imply LLM extraction is complete.
                for path in paths:
                    content = path.read_text(encoding="utf-8").strip()
                    self.workbench.sources.add({
                        "title": path.stem[3:] + "（示例资料）", "text": content,
                        "url": "", "filename": path.name, "kind": "text",
                        "truncated": False, "original_chars": len(content),
                    })
                await self._import(paths)
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(json.dumps(cfg.embedding_identity(), ensure_ascii=False, indent=2), encoding="utf-8")
            await self._refresh_projection()
        except Exception:
            self.client.close()
            self.client = None
            raise
        return self.info()

    def info(self):
        return {
            "workspace": self.settings.workspace, "collection": self.collection,
            "qdrant_mode": self.settings.qdrant_mode,
            "embedding_model": self.settings.embedding_model,
            "embedding_dimension": self.settings.embedding_dim,
            "embedding_provider": self.settings.embedding_provider,
            "total_points": self.total_points, "sample_count": len(self.background),
            "sample_limit": self.settings.sample_limit,
            "variance": self.projection.variance, "projection_note": self.projection.note,
            "lightrag_version": "1.5.7", "qdrant_client_version": "1.19.1",
        }

    async def _import(self, paths):
        chunks = await asyncio.to_thread(read_chunks, paths)
        if not chunks:
            raise ValueError("没有找到非空的 UTF-8 TXT 或 Markdown 内容。")
        # A documented public API for pre-supplied chunks. No entities or
        # relationships are invented, and no LLM/extraction is called.
        await self.rag.ainsert_custom_kg({"chunks": chunks, "entities": [], "relationships": []})
        await self.rag.chunks_vdb.index_done_callback()
        return len(chunks)

    def import_files(self, paths):
        self.ready.result()
        return self._submit_exclusive(self._import_and_refresh, paths)

    async def _import_and_refresh(self, paths):
        count = await self._import(paths)
        await self._refresh_projection()
        return count

    async def _refresh_projection(self):
        started = time.perf_counter()
        records, _ = self.client.scroll(
            collection_name=self.collection, scroll_filter=self.workspace_filter,
            limit=self.settings.sample_limit, with_payload=True, with_vectors=True,
        )
        self.total_points = self.client.count(self.collection, count_filter=self.workspace_filter, exact=True).count
        vectors = [r.vector for r in records]
        self.projection = Projection.fit(vectors, self.settings.embedding_dim)
        coords = self.projection.transform(vectors)
        self.background = [self._plot_point(r, coords[i]) for i, r in enumerate(records)]
        self.background_ids = {p["point_id"] for p in self.background}
        self.projection_read_ms = (time.perf_counter() - started) * 1000
        self.projection_version = time.time_ns()
        return self.info()

    @staticmethod
    def _plot_point(record, coords):
        payload = record.payload or {}
        return {
            "point_id": point_id(record.id), "chunk_id": payload.get("id"),
            "x": float(coords[0]), "y": float(coords[1]),
            "content": payload.get("content", ""), "file_path": payload.get("file_path", "未知来源"),
        }

    def refresh_projection(self):
        self.ready.result()
        return self._submit_exclusive(self._refresh_projection)

    def _submit_exclusive(self, function, *args):
        if not self.gate.acquire(blocking=False):
            raise ValueError("已有任务在执行，请等待完成后再操作。")
        async def guarded():
            try:
                return await function(*args)
            finally:
                self.gate.release()
        return asyncio.run_coroutine_threadsafe(guarded(), self.loop)

    def submit(self, question: str, top_k=5, threshold=0.2):
        self.ready.result()
        question = question.strip()
        if len(question) < 3 or len(question) > 2000:
            raise ValueError("请输入 3–2,000 个字符的问题。")
        if not 1 <= top_k <= 20 or not -1 <= threshold <= 1:
            raise ValueError("Top-K 或相似度阈值超出范围。")
        trace = Trace(question, top_k, threshold, self.info())
        future = self._submit_exclusive(self._query, trace, question, top_k, threshold)
        return future, trace

    async def _query(self, trace, question, top_k, threshold):
        self.active_trace = trace
        trace.emit("query.started", {"mode": "naive", "top_k": top_k, "threshold": threshold, "max_total_tokens": 8000, "rerank": False})
        try:
            self.rag.chunks_vdb.cosine_better_than_threshold = threshold
            query_started = time.perf_counter()
            data = await self.rag.aquery_data(question, QueryParam(
                mode="naive", top_k=top_k, chunk_top_k=top_k,
                max_total_tokens=8000, enable_rerank=False,
            ))
            query_ms = (time.perf_counter() - query_started) * 1000
            if data.get("status") == "failure" and data.get("metadata", {}).get("failure_reason") != "no_results":
                raise ValueError("LightRAG 没有成功返回检索数据，请检查知识库配置。")
            chunks = data.get("data", {}).get("chunks", [])
            final_ids = {c["chunk_id"] for c in chunks}
            search_done = next((e for e in reversed(trace.events) if e["event_type"] == "vector_search.completed"), None)
            context_ms = max(0, (time.perf_counter() - trace.started) * 1000 - search_done["elapsed_ms"]) if search_done else 0
            trace.emit("context.completed", {"chunk_ids": sorted(final_ids), "count": len(chunks), "status": data.get("status")}, context_ms)
            hits = []
            for hit in trace.hits:
                payload = hit["payload"]
                hits.append({
                    **{key: value for key, value in hit.items() if key != "payload"},
                    "content": payload.get("content", ""), "file_path": payload.get("file_path", "未知来源"),
                    "in_context": hit["chunk_id"] in final_ids,
                })
            trace.record.update({
                "status": "success" if hits else "empty", "request": trace.request,
                "hits": hits, "context": data, "query_duration_ms": round(query_ms, 2),
            })
            # Visualization reads are intentionally outside the timed LightRAG retrieval.
            try:
                trace.record["plot"] = self._plot_for_trace(trace)
            except Exception as exc:
                trace.record["plot"] = {"points": self.background, "query": None, "variance": self.projection.variance}
                trace.record["visualization_error"] = safe_error(exc, self.settings)
        except Exception as exc:
            message = safe_error(exc, self.settings)
            trace.emit("query.failed", {"error_type": type(exc).__name__, "message": message})
            trace.record.update({"status": "failed", "error": message, "request": trace.request, "hits": [], "context": {}})
        finally:
            self.active_trace = None
        trace.record["search_calls"] = sum(e["event_type"] == "vector_search.started" for e in trace.events)
        trace.record["embedding_calls"] = sum(e["event_type"] == "embedding.started" for e in trace.events)
        trace.save(self.settings.run_dir)
        return trace.record

    def _plot_for_trace(self, trace):
        started = time.perf_counter()
        extra = []
        # Local Qdrant can distinguish dashed UUID strings from the upstream
        # adapter's compact UUID strings; retrieve with the exact original ID.
        missing = [h["raw_point_id"] for h in trace.hits if h["point_id"] not in self.background_ids]
        if missing:
            records = self.client.retrieve(self.collection, ids=missing, with_payload=True, with_vectors=True)
            coords = self.projection.transform([r.vector for r in records])
            extra = [self._plot_point(r, coords[i]) for i, r in enumerate(records)]
            if len(records) != len(missing):
                raise ValueError("部分命中点在绘图读取前已变化，请暂停写入后刷新背景。")
        q = self.projection.transform([trace.query_vector])[0] if trace.query_vector is not None else None
        return {
            "points": self.background + extra, "query": q.tolist() if q is not None else None,
            "variance": self.projection.variance, "note": self.projection.note,
            "version": self.projection_version, "background_count": len(self.background),
            "extra_count": len(extra), "read_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def close(self):
        async def shutdown():
            if self.rag and self.client:
                await self.rag.finalize_storages()
                self.client.close()
                self.client = None
            # LightRAG's priority queues run worker/health-check tasks on this loop.
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(timeout=30)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()
