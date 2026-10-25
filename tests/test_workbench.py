"""Full native LightRAG ingestion with a fixed LLM test double.

Only the extraction model response is mocked. Graph, embeddings, Qdrant,
document journal, job events, and snapshots are real temporary stores.
This test is NOT evidence that a user's remote model has been connected.
"""
import json
import stat

import pytest
from evidence_helpers import verification_reply

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


def test_independent_semantic_job_preserves_lightrag_state(built_workspace):
    from lab.ontology.profile import uri
    engine, source, _ = built_workspace
    work=engine.workbench
    before=work.sources.get(source['id'])
    original=work.llm._request
    async def response(cfg,messages):
        verification = verification_reply(messages)
        if verification is not None:
            return verification, {}
        return json.dumps({'entities':[{'id':'d','label':'测试文献','type':'E31'},{'id':'t','label':'Saga','type':'E55'}],
                           'statements':[{'subject':'d','predicate':str(uri('P70')),'object':'t','quote':source['text'],'start':0}], 'deferred':[]}),{}
    work.llm._request=response
    try:
        engine.gate.acquire()
        try:
            with pytest.raises(ValueError,match='已有'): work.semantic.start_extraction([source['id']])
            assert not work.semantic.batches()
        finally: engine.gate.release()
        result=work.semantic.start_extraction([source['id']])[0].result(timeout=30)
        assert result['status']=='success'
        assert work.sources.get(source['id'])==before
        values=work.semantic.list_candidates()
        assert len(values)==1 and values[0]['validation']['valid']
        assert len(work.semantic.get_published_graph()['statements']) == 1
        assert values[0]['reviews'][0]['action'] == 'auto_publish'
    finally:
        work.llm._request=original


def test_main_app_automatic_workflow_and_batch_graph(built_workspace, monkeypatch):
    import time
    from streamlit.testing.v1 import AppTest
    from test_demo import fixed_tools
    import lab.engine
    engine, _, _ = built_workspace
    fixed_tools(monkeypatch)
    # Remove the native-ingestion fixture's instance override for this test only.
    monkeypatch.delattr(engine.workbench.llm, '_request')
    monkeypatch.setattr(lab.engine, 'Engine', lambda settings: engine)
    work = engine.workbench
    before = len(work.jobs())
    engine.gate.acquire()
    try:
        with pytest.raises(ValueError, match='已有'):
            work.start('semantic_auto', {'topic': '宋朝文化', 'limit': 3, 'domain': 'culture'})
        assert len(work.jobs()) == before
    finally:
        engine.gate.release()
    app = AppTest.from_file(str(ROOT / 'app.py'))
    app.run(timeout=60)
    assert not app.exception
    # The platform home is the default page; the collection workbench lives under the system menu.
    assert any(b.label == '进入爆款预测' for b in app.button)
    app.query_params['view'] = 'collect'
    app.run(timeout=60)
    assert not app.exception
    assert app.text_input(key='auto-topic').value == '宋朝文化'
    next(b for b in app.button if b.label == '一键搜集并构建本体图谱').click().run(timeout=60)
    assert not app.exception
    # Inspect another main-project page while the background task finishes.
    app.query_params['view'] = 'semantic'
    app.run(timeout=60)
    assert not app.exception
    deadline = time.monotonic() + 30
    while work.active and time.monotonic() < deadline:
        time.sleep(.02)
    assert work.active is None
    job = work.jobs()[0]
    assert job['kind'] == 'semantic_auto'
    assert job['semantic_result']['collected'] == 1
    assert job['semantic_result']['published'] == 1
    batch = job['batch_id']
    app.query_params['batch'] = batch
    app.run(timeout=60)
    assert not app.exception
    assert next(s for s in app.selectbox if s.label == '抽取批次').value == batch
    assert len(app.get('plotly_chart')) == 1
    assert len(app.get('download_button')) >= 3
    assert 'urn:atlas:project:' in work.semantic.export('turtle')
