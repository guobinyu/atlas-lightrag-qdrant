import json

import numpy as np
import pytest
from qdrant_client import QdrantClient, models

from lab.trace import Trace, TracedClient, replay_state, vector_digest


class Spy:
    def __init__(self, client):
        self.client, self.calls, self.last_response = client, 0, None

    def query_points(self, *args, **kwargs):
        self.calls += 1
        self.last_response = self.client.query_points(*args, **kwargs)
        return self.last_response


def test_delegate_matches_unwrapped_qdrant_and_preserves_id_mapping(tmp_path):
    client = QdrantClient(":memory:")
    client.create_collection("chunks", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    client.upsert("chunks", [
        models.PointStruct(id=11, vector=[1., 0., 0.], payload={"workspace_id": "a", "id": "chunk-alpha"}),
        models.PointStruct(id=12, vector=[0., 1., 0.], payload={"workspace_id": "a", "id": "chunk-beta"}),
        models.PointStruct(id=13, vector=[1., 0., 0.], payload={"workspace_id": "b", "id": "chunk-other"}),
    ])
    trace = Trace("query", 2, .2, {})
    spy = Spy(client)
    proxy = TracedClient(spy, lambda: trace)
    kwargs = dict(collection_name="chunks", query=[1., 0., 0.], limit=2, score_threshold=.2, with_payload=True,
                  query_filter=models.Filter(must=[models.FieldCondition(key="workspace_id", match=models.MatchValue(value="a"))]))
    response = proxy.query_points(**kwargs)
    assert response is spy.last_response
    assert spy.calls == 1
    raw = client.query_points(**kwargs)
    assert response == raw
    assert [(h["point_id"], h["chunk_id"], h["score"]) for h in trace.hits] == [("11", "chunk-alpha", 1.0)]
    assert trace.request["limit"] == 2
    assert trace.request["score_threshold"] == .2
    assert trace.request["vector_digest"] == vector_digest(trace.query_vector)
    trace.save(tmp_path)
    data = json.loads((tmp_path / f"{trace.id}.json").read_text())
    assert "query_vector" not in data
    assert "query" not in data["events"][0]["data"]
    client.close()


def test_proxy_preserves_original_exception_and_calls_once():
    original = TimeoutError("original")
    class Broken:
        calls = 0
        def query_points(self, **kwargs):
            self.calls += 1
            raise original
    client = Broken()
    trace = Trace("query", 2, .2, {})
    proxy = TracedClient(client, lambda: trace)
    with pytest.raises(TimeoutError) as caught:
        proxy.query_points(collection_name="x", query=[1., 2.], limit=2)
    assert caught.value is original
    assert client.calls == 1
    assert [e["event_type"] for e in trace.events] == ["vector_search.started"]


def test_replay_is_pure_and_gates_future_events():
    record = {"events": [{"event_type": kind} for kind in ["query.started", "embedding.started", "embedding.completed", "vector_search.started", "vector_search.completed", "context.completed"]]}
    assert not replay_state(record, 2)["show_query"]
    assert replay_state(record, 3)["show_query"]
    assert not replay_state(record, 4)["show_hits"]
    assert replay_state(record, 5)["show_hits"]
    assert not replay_state(record, 5)["show_context"]
    assert replay_state(record, 6)["show_context"]
