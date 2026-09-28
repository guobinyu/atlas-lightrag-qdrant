"""Full native LightRAG ingestion with a fixed LLM test double.

Only the extraction model response is mocked. Graph, embeddings, Qdrant,
document journal, job events, and snapshots are real temporary stores.
This test is NOT evidence that a user's remote model has been connected.
"""
import json
import stat

import pytest

from lab.config import ROOT, Settings
from lab.engine import Engine
from lab.graph import subgraph
from lab.llm import LLMSettings

EXTRACTION = "\n".join([
    "entity<|#|>Saga<|#|>Method<|#|>Saga 把分布式事务拆成本地事务。",
    "entity<|#|>补偿事务<|#|>Method<|#|>补偿事务恢复已完成步骤的业务一致性。",
    "relation<|#|>Saga<|#|>补偿事务<|#|>故障恢复<|#|>Saga 在失败时执行补偿事务。",
    "<|COMPLETE|>",
])


@pytest.fixture(scope="module")
def built_workspace(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-graph")
    (directory / "models").symlink_to(ROOT / "data/models", target_is_directory=True)
    engine = Engine(Settings(data_dir=directory, workspace="graph_fixture", seed_demo=False))
    engine.ready.result(timeout=600)
    engine.workbench.llm.settings = LLMSettings("http", "http://127.0.0.1:9/v1/chat/completions", "test-double", "TEST_SECRET_NOT_REAL")
    calls = []
    async def model_response(cfg, messages):
        calls.append(messages)
        return EXTRACTION, {"input_tokens": 100, "output_tokens": 100}
    engine.workbench.llm._request = model_response
    source = engine.workbench.sources.add({"title": "Saga 测试资料", "url": "", "kind": "text", "text": "Saga 把分布式事务拆成本地事务。每个本地事务提交以后，当后续步骤失败时，Saga 会执行补偿事务来恢复业务一致性。补偿需要考虑幂等性。"})
    yield engine, source, calls
    engine.close()


def test_native_ingestion_builds_graph_and_three_qdrant_collections(built_workspace):
    engine, source, calls = built_workspace
    future, job = engine.workbench.start("build", [source["id"]])
    result = future.result(timeout=120)
    assert result["status"] == "success", result
    assert result["documents"][0]["status"] == "processed"
    assert calls
    data = engine.workbench.cached_snapshot
    assert {n["id"] for n in data["nodes"]} == {"Saga", "补偿事务"}
    assert len(data["edges"]) == 1
    assert data["counts"] == {"chunks": 1, "entities": 2, "relationships": 1}
    events = [e["event_type"] for e in result["events"]]
    for event in ["llm.started", "llm.completed", "embedding.completed", "vectors.written", "document.completed", "build.completed"]:
        assert event in events
    chunk_ids = {p["chunk_id"] for p in data["spaces"]["chunks"]["plot"]["points"]}
    assert all(n["source_id"] in chunk_ids for n in data["nodes"])
    assert all(n["file_path"] == source["file_path"] for n in data["nodes"])
    files = "".join(p.read_text() for p in engine.workbench.job_dir.glob("*.json"))
    assert "TEST_SECRET_NOT_REAL" not in files
    assert "---Input Text---" not in files
    assert engine.workbench.sources.get(source["id"])["status"] == "processed"


def test_duplicate_build_does_not_call_model_again(built_workspace):
    engine, source, calls = built_workspace
    before = len(calls)
    result = engine.workbench.start("build", [source["id"]])[0].result(timeout=60)
    assert result["status"] == "success"
    assert len(calls) == before
    assert result["result"]["vectors"] == {"chunks": 1, "entities": 2, "relationships": 1}


def test_single_job_gate_and_no_phantom_jobs(built_workspace):
    engine, source, calls = built_workspace
    before = len(engine.workbench.jobs())
    engine.gate.acquire()
    try:
        with pytest.raises(ValueError, match="已有"):
            engine.workbench.start("build", [source["id"]])
        assert len(engine.workbench.jobs()) == before
    finally:
        engine.gate.release()


def test_llm_failure_does_not_report_success(built_workspace):
    engine, _, _ = built_workspace
    source = engine.workbench.sources.add({"title": "失败测试", "url": "", "text": "这是故障测试文档，与已有文档不同。模拟模型服务不可用时，系统应该保留失败状态，不能伪造已生成的知识图谱或把文档标成成功。"})
    original = engine.workbench.llm._request
    async def fail(*args, **kwargs):
        raise ValueError("model unavailable")
    engine.workbench.llm._request = fail
    try:
        result = engine.workbench.start("build", [source["id"]])[0].result(timeout=120)
        assert result["status"] == "failed", result
        assert engine.workbench.sources.get(source["id"])["status"] == "failed"
        assert any(e["event_type"] == "llm.failed" for e in result["events"])
    finally:
        engine.workbench.llm._request = original
    with pytest.raises(ValueError, match="重试"):
        engine.workbench.start("build", [source["id"]])
    retried = engine.workbench.start("retry", [])[0].result(timeout=120)
    assert retried["status"] == "success", retried
    assert engine.workbench.sources.get(source["id"])["status"] == "processed"


def test_model_settings_are_private_and_public_view_redacts_key(built_workspace):
    engine, _, _ = built_workspace
    settings = engine.workbench.llm.settings
    engine.workbench.llm.save(settings)
    assert stat.S_IMODE(engine.workbench.llm.path.stat().st_mode) == 0o600
    assert "api_key" not in settings.public()


def test_subgraph_can_find_nodes_outside_default_limit():
    nodes = [{"id": f"n{i}", "entity_type": "Concept"} for i in range(200)]
    edges = [{"source": "n199", "target": "n198"}]
    shown, relations = subgraph(nodes, edges, focus="n199", depth=1, limit=160)
    assert {n["id"] for n in shown} == {"n199", "n198"}
    assert len(relations) == 1
