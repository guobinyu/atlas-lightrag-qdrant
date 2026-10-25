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
        from lab.image_service import ImageService
        self.images = ImageService(self.directory / "image-settings.json")
        from lab.ontology.service import SemanticService
        self.semantic = SemanticService(self.directory / "ontology", self)
        self.semantic.auto_publish()
        from lab.culture.service import CultureService
        self.culture = CultureService(self.directory / "culture")
        from lab.campaigns import CampaignService
        self.campaigns = CampaignService(self)
        self.campaigns.interrupt()
        from lab.demands import DemandService
        self.demands = DemandService(self)
        self.demands.interrupt()
        self.cached_snapshot = None
        # Never automatically resubmit a write that was interrupted by restart.
        for path in self.job_dir.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["status"] == "running":
                value["status"] = "interrupted"
                value["message"] = ("进程已中断。已采集资料和成功文本块保留；在校验报告中重试失败或中断块。尚未创建批次时请重新启动任务。"
                                    if value['kind'].startswith('semantic') else
                                    "进程已中断。请刷新资料状态，确认 LightRAG 的实际文档状态后再重试。")
                if value['kind'] == 'image_test':
                    value['message'] = '测试图任务已中断；可重新测试，服务端可能已产生费用。'
                if value['kind'].startswith('campaign_'):
                    value['message'] = '营销任务已中断；已保存资料和版本，可在热点营销页重试失败阶段。'
                if value['kind'].startswith('demand_'):
                    value['message'] = '需求任务已中断；可在产品需求探索页重试已保存阶段。'
                if value['kind'].startswith('culture_'):
                    value['message'] = '文化解码已中断；已入库素材保留，可在文化知识库页重试未完成文本块。'
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def emit(self, event, data):
        if self.active:
            self.active.emit(event, data)

    def jobs(self):
        values = [json.loads(p.read_text(encoding="utf-8")) for p in self.job_dir.glob("*.json")]
        return sorted(values, key=lambda item: item["created_at"], reverse=True)[:30]

    def start(self, kind, inputs):
        self.engine.ready.result()
        if kind == 'image_test':
            return self._start_image_test()
        if kind.startswith('demand_'):
            return self._start_demand(kind, inputs)
        if kind in {'campaign_search', 'campaign_generate', 'campaign_revise', 'campaign_retry', 'campaign_evergreen', 'campaign_recheck'}:
            return self._start_campaign(kind, inputs)
        if kind in {"semantic", "semantic_retry", "semantic_auto"}:
            return self._start_semantic(kind, inputs)
        if kind in {"culture_decode", "culture_retry", "culture_auto"}:
            return self._start_culture(kind, inputs)
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

    def configure_images(self, settings):
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError('当前工作区有任务执行中，请稍后保存配置。')
        try:
            self.images.save(settings)
        finally:
            self.engine.gate.release()

    def _start_image_test(self):
        self.images.settings.validate()
        if not self.images.configured:
            raise ValueError('请先保存图片接口与密钥。')
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError('当前工作区有任务执行中，请稍后测试。')
        try:
            job = Job(self.job_dir, 'image_test', [])
            self.active = job
            async def execute():
                try:
                    from lab.image_service import image_sizes, write_png
                    raw = await self.images.render('平面概念纹样：米白背景，墨绿细线水波与简洁桥拱，无文字，无摄影背景。', image_sizes(self.images.settings.provider)[0])
                    relative = 'image-tests/' + job.value['id'] + '.png'
                    write_png(self.directory / relative, raw)
                    job.update(status='success', image_path=relative, message='测试图已生成，图片接口可用。')
                except Exception as exc:
                    from lab.engine import safe_error
                    message = safe_error(exc, self.engine.settings)
                    if self.images.settings.api_key:
                        message = message.replace(self.images.settings.api_key, '[redacted]')
                    job.update(status='failed', message=message)
                finally:
                    job.update(finished_at=utc_now())
                    self.active = None
                    self.engine.gate.release()
                return job.snapshot()
            return asyncio.run_coroutine_threadsafe(execute(), self.engine.loop), job
        except Exception:
            self.active = None
            self.engine.gate.release()
            raise

    def _start_demand(self, kind, inputs):
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError('当前工作区有任务执行中，请稍后继续。')
        try:
            effective = kind
            cached_prompts = False
            if kind == 'demand_retry':
                prior = self.demands.load(inputs.get('run_id'))['operations'].get(inputs.get('operation_id'), {})
                effective = prior.get('kind')
                cached_prompts = bool({'image_prompts', 'pattern_prompts'} & set(prior.get('cache', {})))
            if effective != 'demand_image_revise' and not (effective == 'demand_image_generate' and cached_prompts):
                self.llm.settings.validate()
            run, operation = self.demands.prepare(kind, inputs)
            job = Job(self.job_dir, kind, [])
            operation['job_id'] = job.value['id']
            self.demands.save(run)
            job.update(demand_id=run['id'], operation_id=operation['id'], phase=operation['stage'])
            self.active = job

            async def execute():
                try:
                    await self.demands.execute(run, operation, job)
                except Exception as exc:
                    from lab.engine import safe_error
                    message = safe_error(exc, self.engine.settings)
                    if self.llm.settings.api_key:
                        message = message.replace(self.llm.settings.api_key, '[redacted]')
                    if self.images.settings.api_key:
                        message = message.replace(self.images.settings.api_key, '[redacted]')
                    operation.update(status='failed', message=message, finished_at=utc_now())
                    self.demands.save(run)
                    job.emit('job.failed', {'message': message, 'error_type': type(exc).__name__})
                    job.update(status='failed', message=message)
                finally:
                    job.update(finished_at=utc_now())
                    self.active = None
                    self.engine.gate.release()
                return job.snapshot()

            return asyncio.run_coroutine_threadsafe(execute(), self.engine.loop), job
        except Exception:
            self.active = None
            self.engine.gate.release()
            raise

    def _start_campaign(self, kind, inputs):
        self.llm.settings.validate()
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError('已有检索或构建在执行，请等待完成。')
        try:
            run, operation = self.campaigns.prepare(kind, inputs)
            job = Job(self.job_dir, kind, [])
            operation['job_id'] = job.value['id']
            self.campaigns.save(run)
            job.update(campaign_id=run['id'], operation_id=operation['id'], phase=operation['stage'])
            self.active = job

            async def execute():
                try:
                    await self.campaigns.execute(run, operation, job)
                except Exception as exc:
                    from lab.engine import safe_error
                    message = safe_error(exc, self.engine.settings)
                    if self.llm.settings.api_key:
                        message = message.replace(self.llm.settings.api_key, '[redacted]')
                    operation.update(status='failed', message=message, finished_at=utc_now())
                    self.campaigns.save(run)
                    job.emit('job.failed', {'message': message, 'error_type': type(exc).__name__})
                    job.update(status='failed', message=message)
                finally:
                    job.update(finished_at=utc_now())
                    self.active = None
                    self.engine.gate.release()
                return job.snapshot()

            return asyncio.run_coroutine_threadsafe(execute(), self.engine.loop), job
        except Exception:
            self.active = None
            self.engine.gate.release()
            raise

    def _start_semantic(self, kind, inputs):
        self.llm.settings.validate()
        if kind == 'semantic_auto':
            from lab.ontology.profile import DOMAINS
            if (not isinstance(inputs.get('topic'), str) or not 2 <= len(inputs['topic'].strip()) <= 300
                    or type(inputs.get('limit')) is not int or not 1 <= inputs['limit'] <= 10
                    or inputs.get('domain', 'culture') not in DOMAINS):
                raise ValueError('请输入 2–300 字符的主题，数量为 1–10，并选择有效领域。')
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError("已有检索或构建在执行，请等待完成。")
        try:
            model = self.llm.settings.public()
            if kind == 'semantic_auto':
                ids = []
                batch_id = None
            elif kind == "semantic":
                ids = inputs["source_ids"]
                sources = [self.sources.get(identifier) for identifier in ids]
                batch_id = self.semantic.create_batch(sources, model, inputs["domain"])
            else:
                batch_id = inputs["batch_id"]
                batch = next((b for b in self.semantic.batches() if b["id"] == batch_id), None)
                if not batch:
                    raise ValueError("未知语义抽取批次")
                ids = [s["id"] for s in batch["sources"]]
                self.semantic.prepare_retry(batch_id, [self.sources.get(i) for i in ids], model)
            job = Job(self.job_dir, kind, ids)
            job.update(batch_id=batch_id, model=model, publication_policy='automatic-evidence-v2')
            if kind == 'semantic_auto':
                job.update(topic=inputs['topic'].strip(), requested=inputs['limit'], domain=inputs.get('domain', 'culture'))
            self.active = job
            async def run():
                try:
                    if kind == 'semantic_auto':
                        from lab.demo import build_demo
                        def progress(event, data):
                            job.emit(event, data)
                            if event == 'semantic.batch.created':
                                job.update(**data)
                            elif event == '资料已保存':
                                job.update(collected=data['count'])
                        result = await build_demo(inputs['topic'], inputs['limit'], self.llm, self.sources,
                                                  self.semantic, progress, inputs.get('domain', 'culture'), job.value['id'])
                        job.update(status=result['status'], semantic_result=result, batch_id=result['batch_id'], source_ids=result['source_ids'])
                    else:
                        state = await self.semantic.run(batch_id, self.llm, job.emit)
                        claims = self.semantic.list_candidates(batch_id=batch_id)
                        job.update(status=state, semantic_result={'batch_id': batch_id, 'collected': len(ids),
                            'extracted': len(claims), 'published': sum(c['status'] == 'published' for c in claims),
                            'blocked': sum(c['status'] == 'pending' for c in claims),
                            'failed_blocks': sum(b['status'] == 'failed' for b in self.semantic.report(batch_id))})
                    job.update(message="任务结束；本体校验和模型证据检查均通过的陈述已自动发布。数量缺口和未发布原因请查看结果。")
                except Exception as exc:
                    job.update(status="failed", message=type(exc).__name__ + "：语义任务失败，详情请查看校验报告。")
                finally:
                    job.update(finished_at=utc_now())
                    self.active = None
                    self.engine.gate.release()
                return job.snapshot()
            return asyncio.run_coroutine_threadsafe(run(), self.engine.loop), job
        except Exception:
            self.active = None
            self.engine.gate.release()
            raise

    def _start_culture(self, kind, inputs):
        self.llm.settings.validate()
        if kind == 'culture_auto' and (not isinstance(inputs.get('topic'), str) or not 2 <= len(inputs['topic'].strip()) <= 300
                                       or type(inputs.get('limit')) is not int or not 1 <= inputs['limit'] <= 10):
            raise ValueError('请输入 2–300 字符的主题，数量为 1–10。')
        if not self.engine.gate.acquire(blocking=False):
            raise ValueError("已有检索或构建在执行，请等待完成。")
        try:
            model = self.llm.settings.public()
            ids, batch_id = [], None
            if kind == 'culture_decode':
                ids = inputs['source_ids']
                batch_id = self.culture.create_batch([self.sources.get(i) for i in ids], model)
            elif kind == 'culture_retry':
                batch_id = inputs['batch_id']
                ids = [s['id'] for s in self.culture.batch(batch_id)['sources']]
                self.culture.prepare_retry(batch_id, [self.sources.get(i) for i in ids], model)
            job = Job(self.job_dir, kind, ids)
            job.update(batch_id=batch_id, model=model)
            if kind == 'culture_auto':
                job.update(topic=inputs['topic'].strip(), requested=inputs['limit'])
            self.active = job
            async def run():
                try:
                    bid, partial = batch_id, False
                    if kind == 'culture_auto':
                        from lab.demo import collect
                        def progress(event, data):
                            job.emit(event, data)
                            if event == '资料已保存':
                                job.update(collected=data['count'])
                        topic = inputs['topic'].strip()
                        queries, collected, failures = await collect(topic, inputs['limit'], self.llm, self.sources, progress)
                        job.update(source_ids=[s['id'] for s in collected], queries=queries, failures=failures)
                        if not collected:
                            job.update(status='failed', message='没有读取到可用正文，未创建解码批次。可调整主题后重试。')
                            return job.snapshot()
                        bid = self.culture.create_batch(collected, model, topic=topic)
                        partial = len(collected) < inputs['limit']
                        job.update(batch_id=bid)
                    state = await self.culture.run(bid, self.llm, job.emit)
                    job.emit('culture.index.started', {})
                    index = await self.culture.sync(self.engine.culture_index)
                    job.update(status='partial' if partial and state == 'success' else state,
                               culture_result={**self.culture.summary(bid), 'index': index},
                               message='解码结束；引文能在原文定位的素材与关系已入库并更新向量索引。未入库原因请在批次记录中查看。')
                except Exception as exc:
                    from lab.engine import safe_error
                    message = safe_error(exc, self.engine.settings)
                    if self.llm.settings.api_key:
                        message = message.replace(self.llm.settings.api_key, '[redacted]')
                    job.emit('job.failed', {'message': message, 'error_type': type(exc).__name__})
                    job.update(status='failed', message=message)
                finally:
                    job.update(finished_at=utc_now())
                    self.active = None
                    self.engine.gate.release()
                return job.snapshot()
            return asyncio.run_coroutine_threadsafe(run(), self.engine.loop), job
        except Exception:
            self.active = None
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
