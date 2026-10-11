"""Real LightRAG/Qdrant retrieval inside the already-held marketing task gate."""
import asyncio

from lab.config import ROOT, Settings
from lab.engine import Engine
from lab.llm import LLMSettings
from test_workbench import EXTRACTION


def test_campaign_context_reuses_real_index_without_nested_gate(tmp_path):
    # LightRAG keeps process-global workspace state: use our own workspace name,
    # not the native-ingestion fixture's name, even in a different directory.
    (tmp_path / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    engine = Engine(Settings(data_dir=tmp_path, workspace='campaign_context_fixture', seed_demo=False))
    try:
        engine.ready.result(timeout=120)
        engine.workbench.llm.settings = LLMSettings('http', 'http://127.0.0.1:9/v1/chat/completions', 'test-double', '')
        async def response(*args):
            return EXTRACTION, {}
        engine.workbench.llm._request = response
        source = engine.workbench.sources.add({'title': '营销检索测试', 'url': '', 'text': 'Saga 把分布式事务拆成本地事务。当后续步骤失败时，Saga 会执行补偿事务来恢复业务一致性。补偿需要考虑幂等性。'})
        result = engine.workbench.start('build', [source['id']])[0].result(timeout=120)
        assert result['status'] == 'success'
        engine.gate.acquire()
        try:
            engine.rag.chunks_vdb.cosine_better_than_threshold = 0.99
            chunks = asyncio.run_coroutine_threadsafe(engine.campaign_context('Saga 补偿事务'), engine.loop).result(timeout=30)
            assert chunks and len(chunks) <= 5
            assert all(c['content'] and c['chunk_id'] for c in chunks)
            assert any(source['file_path'] == c['file_path'] for c in chunks)
            assert engine.rag.chunks_vdb.cosine_better_than_threshold == 0.99
        finally:
            engine.gate.release()
    finally:
        engine.close()
