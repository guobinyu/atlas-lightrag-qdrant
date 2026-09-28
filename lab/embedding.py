import asyncio
import time

import httpx
import numpy as np

from lab.trace import vector_digest


class Embedding:
    def __init__(self, settings, current_trace):
        self.settings = settings
        self.current_trace = current_trace
        self.call_count = 0
        self.model = None
        if settings.embedding_provider == "fastembed":
            from fastembed import TextEmbedding
            self.model = TextEmbedding(
                model_name=settings.embedding_model,
                cache_dir=str(settings.data_dir / "models"), threads=2,
            )

    async def __call__(self, texts: list[str], context="document", **kwargs):
        trace = self.current_trace()
        self.call_count += 1
        started = time.perf_counter()
        if trace:
            trace.emit("embedding.started", {
                "model": self.settings.embedding_model, "context": context,
                "text_count": len(texts), "dimension": self.settings.embedding_dim,
            })
        prefix = self.settings.query_prefix if context == "query" else self.settings.document_prefix
        inputs = [prefix + text for text in texts]
        if self.model is not None:
            # query_embed/passage_embed preserve the selected model's native preprocessing.
            embed = self.model.query_embed if context == "query" else self.model.passage_embed
            values = await asyncio.to_thread(lambda: np.asarray(list(embed(inputs)), dtype=np.float32))
        else:
            headers = {"Authorization": f"Bearer {self.settings.embedding_api_key}"} if self.settings.embedding_api_key else {}
            async with httpx.AsyncClient(timeout=self.settings.timeout) as client:
                response = await client.post(self.settings.embedding_url, headers=headers, json={
                    "model": self.settings.embedding_model, "input": inputs,
                })
                response.raise_for_status()
                entries = sorted(response.json()["data"], key=lambda item: item["index"])
                if [item["index"] for item in entries] != list(range(len(inputs))):
                    raise ValueError("Embedding 响应中的索引缺失或重复。")
                values = np.asarray([item["embedding"] for item in entries], dtype=np.float32)
        expected = (len(texts), self.settings.embedding_dim)
        if values.shape != expected or not np.isfinite(values).all():
            raise ValueError(f"Embedding 返回不合法，需要形状 {expected} 的有限数值。")
        if np.any(np.linalg.norm(values, axis=1) < 1e-12):
            raise ValueError("Embedding 返回了零向量。")
        if trace:
            trace.emit("embedding.completed", {
                "model": self.settings.embedding_model, "dimension": values.shape[1],
                "preview": values[0, :8].tolist(), "vector_digest": vector_digest(values[0]),
                "context": context,
            }, (time.perf_counter() - started) * 1000)
        return values
