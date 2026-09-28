"""Capture an actual call once; never manufacture retrieval events."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Queue
import time
import uuid

import numpy as np


def point_id(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return str(value)


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def vector_digest(vector) -> str:
    return hashlib.sha256(np.asarray(vector, dtype="<f4").tobytes()).hexdigest()[:16]


class Trace:
    def __init__(self, question, top_k, threshold, config):
        self.id = uuid.uuid4().hex
        self.started = time.perf_counter()
        self.events = []
        self.queue = Queue()
        self.query_vector = None
        self.hits = []
        self.request = {}
        self.record = {
            "schema_version": 1, "query_id": self.id,
            "question": question, "top_k": top_k, "threshold": threshold,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": config, "events": self.events,
        }

    def emit(self, kind, data=None, duration_ms=None):
        event = {
            "query_id": self.id, "seq": len(self.events) + 1,
            "event_type": kind,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 2),
            "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "data": data or {},
        }
        self.events.append(event)
        self.queue.put(event)

    def save(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        # No full query vectors, credentials, or client objects enter replay files.
        record_path = directory / f"{self.id}.json"
        temp = record_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.record, ensure_ascii=False, indent=2, default=plain), encoding="utf-8")
        temp.replace(record_path)
        (directory / f"{self.id}.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False, default=plain) + "\n" for e in self.events),
            encoding="utf-8",
        )


class TracedClient:
    """Instance-local client delegate. Its owner closes the shared client."""
    def __init__(self, client, current_trace, observer=None):
        self.client = client
        self.current_trace = current_trace
        self.search_count = 0
        self.observer = observer

    def upsert(self, *args, **kwargs):
        result = self.client.upsert(*args, **kwargs)
        if self.observer:
            points = kwargs.get("points", args[1] if len(args) > 1 else [])
            self.observer("vectors.written", {"collection": kwargs.get("collection_name", args[0] if args else ""), "points": len(points), "operation_status": str(result.status)})
        return result

    def __getattr__(self, name):
        return getattr(self.client, name)

    def close(self):
        # LightRAG owns three adapters; Engine owns their shared client.
        pass

    def query_points(self, *args, **kwargs):
        trace = self.current_trace()
        self.search_count += 1
        if trace is None:
            return self.client.query_points(*args, **kwargs)
        query_vector = np.asarray(kwargs["query"], dtype=np.float32)
        trace.query_vector = query_vector.copy()
        request = {
            key: plain(kwargs[key])
            for key in ("collection_name", "limit", "score_threshold", "query_filter", "search_params", "with_payload", "with_vectors")
            if key in kwargs
        }
        request.update({"metric": "Cosine", "vector_dimension": len(query_vector), "vector_digest": vector_digest(query_vector)})
        trace.request = request
        trace.emit("vector_search.started", request)
        started = time.perf_counter()
        # Exceptions propagate unchanged; the job boundary records a safe error.
        response = self.client.query_points(*args, **kwargs)
        trace.hits = [
            {"point_id": point_id(p.id), "raw_point_id": p.id, "chunk_id": (p.payload or {}).get("id"),
             "rank": rank, "score": float(p.score), "payload": p.payload or {}}
            for rank, p in enumerate(response.points, 1)
        ]
        trace.emit("vector_search.completed", {
            "count": len(trace.hits),
            "hits": [{k: v for k, v in h.items() if k != "payload"} for h in trace.hits],
        }, (time.perf_counter() - started) * 1000)
        return response


def list_runs(directory: Path):
    runs = []
    if directory.exists():
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                runs.append(data)
            except (OSError, ValueError):
                continue
    return runs


def replay_state(record: dict, step: int):
    """Pure replay: receives only a saved record, never an Engine or client."""
    events = record["events"][:step]
    kinds = {e["event_type"] for e in events}
    return {
        "events": events,
        "show_query": "embedding.completed" in kinds,
        "show_hits": "vector_search.completed" in kinds,
        "show_context": "context.completed" in kinds,
    }
