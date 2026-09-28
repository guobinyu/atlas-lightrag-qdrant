"""Real LightRAG + real local ONNX model + persistent Qdrant integration."""
from dataclasses import replace
import json
import uuid

import numpy as np
import pytest
from qdrant_client import models

from lab.config import ROOT, Settings
from lab.engine import Engine
from lab.trace import list_runs, replay_state, vector_digest


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("live-rag")
    cache = ROOT / "data/models"
    if cache.exists():
        (data_dir / "models").symlink_to(cache, target_is_directory=True)
    cfg = Settings(data_dir=data_dir, workspace="integration_test", sample_limit=2)
    value = Engine(cfg)
    value.ready.result(timeout=600)
    yield value
    value.close()


def run(engine, question="缂丝为什么称为通经断纬？", top_k=5, threshold=.2):
    future, trace = engine.submit(question, top_k, threshold)
    return future.result(timeout=90), trace


def test_real_retrieval_and_projection_supplement(engine):
    record, trace = run(engine)
    assert record["status"] == "success", record.get("error")
    assert record["search_calls"] == record["embedding_calls"] == 1
    assert len(record["hits"]) == 5
    assert "通经断纬" in record["hits"][0]["content"]
    assert record["plot"]["background_count"] == 2
    assert record["plot"]["extra_count"] >= 3
    plotted = {p["point_id"] for p in record["plot"]["points"]}
    assert all(h["point_id"] in plotted for h in record["hits"])
    final = {c["chunk_id"] for c in record["context"]["data"]["chunks"]}
    assert all(h["in_context"] == (h["chunk_id"] in final) for h in record["hits"])
    assert all(h["point_id"] != h["chunk_id"] for h in record["hits"])
    scores = [h["score"] for h in record["hits"]]
    assert scores == sorted(scores, reverse=True)
    embedding_event = next(e for e in record["events"] if e["event_type"] == "embedding.completed")
    assert embedding_event["data"]["vector_digest"] == record["request"]["vector_digest"] == vector_digest(trace.query_vector)
    np.testing.assert_allclose(record["plot"]["query"], engine.projection.transform([trace.query_vector])[0])
    assert record["request"]["query_filter"]["must"][0]["match"]["value"] == "integration_test"


def test_topk_threshold_no_hits_and_replay_no_calls(engine):
    record, trace = run(engine, top_k=2, threshold=.6)
    assert len(record["hits"]) <= 2
    assert all(h["score"] >= .6 for h in record["hits"])
    empty, _ = run(engine, threshold=1.0)
    assert empty["status"] == "empty"
    assert empty["hits"] == []
    before = (engine.embedder.call_count, engine.traced_client.search_count)
    for saved in list_runs(engine.settings.run_dir):
        for step in range(len(saved["events"]) + 1):
            replay_state(saved, step)
    assert before == (engine.embedder.call_count, engine.traced_client.search_count)


def test_other_workspace_is_excluded_even_with_perfect_match(engine):
    base, trace = run(engine)
    foreign_id = str(uuid.uuid4())
    async def add_foreign():
        engine.client.upsert(engine.collection, [models.PointStruct(
            id=foreign_id, vector=trace.query_vector.tolist(),
            payload={"workspace_id": "different_workspace", "id": "foreign-chunk", "content": "foreign"},
        )])
    engine._submit_exclusive(add_foreign).result(timeout=30)
    again, _ = run(engine)
    assert all(h["point_id"] != foreign_id for h in again["hits"])
    assert [h["chunk_id"] for h in base["hits"]] == [h["chunk_id"] for h in again["hits"]]


def test_embedding_failure_is_recorded_without_secrets(engine):
    original = engine.embedder.model
    class FailingModel:
        def query_embed(self, *args, **kwargs):
            raise ValueError("embedding unavailable")
    engine.embedder.model = FailingModel()
    try:
        record, _ = run(engine)
        assert record["status"] == "failed"
        assert record["events"][-1]["event_type"] == "query.failed"
        assert record["search_calls"] == 0
        assert record["embedding_calls"] == 1
    finally:
        engine.embedder.model = original


def test_reject_changed_embedding_identity(engine):
    path = engine.settings.rag_dir / engine.settings.workspace / "retrieval_lab_embedding.json"
    assert json.loads(path.read_text())["dimension"] == 512
    changed = Engine(replace(engine.settings, embedding_dim=384))
    try:
        with pytest.raises(ValueError, match="Embedding"):
            changed.ready.result(timeout=10)
    finally:
        changed.close()
