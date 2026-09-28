"""Persistent collection/build jobs and snapshots of the actual LightRAG stores."""
import asyncio
from copy import deepcopy
import json
import threading
import uuid

from lightrag.base import DocStatus
from lightrag.kg.shared_storage import initialize_pipeline_status

from lab.llm import ExtractionLLM
from lab.projection import Projection
from lab.sources import SourceStore, fetch_source, utc_now


class Job:
    def __init__(self, directory, kind, source_ids):
        self.lock = threading.RLock()
        self.path = directory / (uuid.uuid4().hex + ".json")
        self.value = {"id": self.path.stem, "kind": kind, "status": "running", "source_ids": source_ids,
                      "created_at": utc_now(), "events": [], "documents": []}
        self.emit("job.started", {"kind": kind, "count": len(source_ids)})

    def emit(self, event, data=None):
        with self.lock:
            self.value["events"].append({"seq": len(self.value["events"]) + 1, "event_type": event, "at": utc_now(), "data": data or {}})
            self._save()

    def update(self, **data):
        with self.lock:
            self.value.update(data)
            self._save()

    def snapshot(self):
        with self.lock:
            return deepcopy(self.value)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


class Workspace:
    def __init__(self, engine):
        self.engine = engine
        self.directory = engine.settings.data_dir / "workbench" / engine.settings.workspace
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sources = SourceStore(self.directory / "sources")
        self.job_dir = self.directory / "jobs"
        self.job_dir.mkdir(exist_ok=True)
        self.active = None
        self.llm = ExtractionLLM(self.directory / "llm-settings.json", self.emit)
        self.cached_snapshot = None
        # Never automatically resubmit a write that was interrupted by restart.
        for path in self.job_dir.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["status"] == "running":
                value["status"] = "interrupted"
                value["message"] = "进程已中断。请刷新资料状态，确认 LightRAG 的实际文档状态后再重试。"
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def emit(self, event, data):
        if self.active:
            self.active.emit(event, data)

    def jobs(self):
        values = [json.loads(p.read_text(encoding="utf-8")) for p in self.job_dir.glob("*.json")]
        return sorted(values, key=lambda item: item["created_at"], reverse=True)[:30]

    def start(self, kind, inputs):
        self.engine.ready.result()
        if kind not in {"collect", "build", "retry"}:
            raise ValueError("未知工作类型。")
        if kind != "retry" and not 1 <= len(inputs) <= 10:
            raise ValueError("每批选择 1–10 份资料。")
        if kind in {"build", "retry"}:
            self.llm.settings.validate()
        if kind == "build":
            if any(self.sources.get(identifier)["status"] in {"failed", "interrupted", "processing", "indexing"} for identifier in inputs):
                raise ValueError("存在失败或中断的文档，请先使用「重试工作区未完成文档」。LightRAG 不会将重复上传自动视为重试。")
            if sum(self.sources.get(identifier)["chars"] for identifier in inputs) > 100000:
                raise ValueError("每批正文总量不能超过 100,000 字符，请分批构建。")
        # Gate first, then create the job: a duplicate click cannot persist a
        # fictitious running job or start a second ingest.
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError("已有检索或构建在执行，请等待完成。")
        try:
            job = Job(self.job_dir, kind, inputs if kind != "collect" else [s["url"] for s in inputs])
            self.active = job
            async def run():
                try:
                    if kind == "collect":
                        await self._collect(job, inputs)
                    elif kind == "retry":
                        await self._retry(job)
                    else:
                        await self._build(job, inputs)
                except Exception as exc:
                    from lab.engine import safe_error
                    message = safe_error(exc, self.engine.settings).replace(self.llm.settings.api_key, "[redacted]") if self.llm.settings.api_key else safe_error(exc, self.engine.settings)
                    job.emit("job.failed", {"message": message, "error_type": type(exc).__name__})
                    job.update(status="failed", message=message)
                    if kind in {"build", "retry"}:
                        for item in self.sources.list():
                            if item["status"] == "indexing" and item.get("job_id") == job.value["id"]:
                                self.sources.update(item["id"], status="interrupted")
                finally:
                    job.update(finished_at=utc_now())
                    self.active = None
                    self.engine.gate.release()
                return job.snapshot()
            future = asyncio.run_coroutine_threadsafe(run(), self.engine.loop)
            return future, job
        except Exception:
            self.engine.gate.release()
            raise

    async def _collect(self, job, candidates):
        documents = []
        for candidate in candidates:
            job.emit("source.fetching", {"title": candidate["title"], "url": candidate["url"]})
            try:
                source = await asyncio.to_thread(fetch_source, candidate["url"], candidate["title"])
                item = self.sources.add(source)
                documents.append({"id": item["id"], "title": item["title"], "status": "collected"})
                job.emit("source.collected", {"id": item["id"], "title": item["title"], "chars": item["chars"], "truncated": item["truncated"]})
            except Exception as exc:
                documents.append({"title": candidate["title"], "status": "failed", "error": f"{type(exc).__name__}：未能取得正文，请使用直接上传。"})
                job.emit("source.failed", documents[-1])
            job.update(documents=documents)
        success = sum(d["status"] == "collected" for d in documents)
        job.update(status="success" if success == len(documents) else "partial" if success else "failed")
        job.emit("collection.completed", {"collected": success, "failed": len(documents) - success})

    async def _build(self, job, source_ids):
        rag = self.engine.rag
        await initialize_pipeline_status(workspace=self.engine.settings.workspace)
        before = await self.snapshot_async()
        job.update(model=self.llm.settings.public())
        documents = []
        for identifier in source_ids:
            item = self.sources.get(identifier)
            self.sources.update(identifier, status="indexing", job_id=job.value["id"])
            job.emit("document.started", {"id": identifier, "title": item["title"], "chars": item["chars"]})
            existing = await rag.doc_status.get_by_id(item["doc_id"])
            # Journaled native ingestion performs chunking, extraction, merging,
            # graph persistence and all three Qdrant vector upserts.
            if not existing or str(existing.get("status")) not in {"processed", "DocStatus.PROCESSED"}:
                await rag.ainsert(item["text"], ids=item["doc_id"], file_paths=item["file_path"], track_id="build-" + job.value["id"])
            state = await rag.doc_status.get_by_id(item["doc_id"])
            status = state.get("status") if state else None
            processed = status == DocStatus.PROCESSED or status == "processed"
            document = {"id": identifier, "title": item["title"], "doc_id": item["doc_id"],
                        "status": "processed" if processed else "failed", "chunks": (state or {}).get("chunks_count", 0)}
            if not processed:
                document["error"] = "LightRAG 未将文档标记为 processed；请检查模型连接、输出格式或重试。"
            self.sources.update(identifier, **{k: v for k, v in document.items() if k not in {"id", "title"}})
            documents.append(document)
            job.update(documents=documents)
            job.emit("document.completed" if processed else "document.failed", document)
        job.emit("indexes.refreshing", {})
        await self.engine._refresh_projection()
        after = await self.snapshot_async()
        done = sum(d["status"] == "processed" for d in documents)
        counts = {"entities": len(after["nodes"]), "relations": len(after["edges"]), "vectors": after["counts"],
                  "new_entities": len(after["nodes"]) - len(before["nodes"]), "new_relations": len(after["edges"]) - len(before["edges"])}
        job.update(status="success" if done == len(documents) else "partial" if done else "failed", result=counts)
        job.emit("build.completed", {"processed": done, "failed": len(documents) - done, **counts})

    async def _retry(self, job):
        # The upstream retry protocol is workspace-wide. The UI explicitly names
        # this scope instead of retrying unrelated documents on a selected build.
        from lightrag.kg.shared_storage import commit_manual_retry_request, get_namespace_data, get_namespace_lock, get_pipeline_ingress
        rag = self.engine.rag
        await initialize_pipeline_status(workspace=rag.workspace)
        statuses = [s for s in DocStatus if s.value in {"failed", "pending", "processing", "parsing", "analyzing"}]
        documents = await rag.doc_status.get_docs_by_statuses(statuses)
        if not documents:
            job.update(status="success", message="当前工作区没有失败或未完成的文档。")
            return
        job.emit("retry.started", {"scope": "entire_workspace", "document_count": len(documents)})
        status = await get_namespace_data("pipeline_status", workspace=rag.workspace)
        lock = get_namespace_lock("pipeline_status", workspace=rag.workspace)
        ingress = await get_pipeline_ingress(rag.workspace)
        refusal = await commit_manual_retry_request(status, lock, ingress, job.value["id"], {})
        if refusal:
            raise ValueError("LightRAG 当前拒绝重试，请检查工作区是否正在恢复或被其他进程占用。")
        await rag.apipeline_process_enqueue_documents()
        outcomes = []
        for doc_id, old in documents.items():
            current = await rag.doc_status.get_by_id(doc_id)
            done = current and current.get("status") in {"processed", DocStatus.PROCESSED}
            outcomes.append({"doc_id": doc_id, "title": old.file_path, "status": "processed" if done else "failed"})
        await self.engine._refresh_projection()
        await self.snapshot_async()
        done = sum(d["status"] == "processed" for d in outcomes)
        job.update(status="success" if done == len(outcomes) else "partial" if done else "failed", documents=outcomes)
        job.emit("retry.completed", {"processed": done, "failed": len(outcomes) - done})

    def refresh(self):
        self.engine.ready.result()
        return self.engine._submit_exclusive(self.snapshot_async)

    async def snapshot_async(self):
        rag = self.engine.rag
        nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
        edges = await rag.chunk_entity_relation_graph.get_all_edges()
        sources = self.sources.list()
        for item in sources:
            state = await rag.doc_status.get_by_id(item["doc_id"])
            if state:
                status = state.get("status")
                status = status.value if hasattr(status, "value") else status
                self.sources.update(item["id"], status=status, chunks=state.get("chunks_count", 0))
            elif item["status"] == "indexing" and self.active is None:
                self.sources.update(item["id"], status="interrupted")
        vector_spaces, counts = {}, {}
        for name, adapter in [("chunks", rag.chunks_vdb), ("entities", rag.entities_vdb), ("relationships", rag.relationships_vdb)]:
            records, _ = self.engine.client.scroll(adapter.final_namespace, scroll_filter=self.engine.workspace_filter, limit=self.engine.settings.sample_limit, with_payload=True, with_vectors=True)
            count = self.engine.client.count(adapter.final_namespace, count_filter=self.engine.workspace_filter, exact=True).count
            counts[name] = count
            projection = Projection.fit([r.vector for r in records], self.engine.settings.embedding_dim)
            coords = projection.transform([r.vector for r in records])
            points = []
            for i, record in enumerate(records):
                point = self.engine._plot_point(record, coords[i])
                point["payload"] = record.payload
                point["dimension"] = len(record.vector)
                points.append(point)
            vector_spaces[name] = {"collection": adapter.final_namespace, "total": count, "plot": {"points": points, "query": None, "variance": projection.variance, "note": projection.note, "version": utc_now()}}
        value = {"nodes": nodes, "edges": edges, "counts": counts, "spaces": vector_spaces, "updated_at": utc_now()}
        self.cached_snapshot = value
        return value

    def configure_llm(self, settings, check=True):
        async def configure():
            result = await self.llm.check(settings) if check else {}
            self.llm.save(settings)
            return result
        return self.engine._submit_exclusive(configure)
