import asyncio
import json

from streamlit.testing.v1 import AppTest

from lab import demo
from lab.config import ROOT
from lab.llm import ExtractionLLM, LLMSettings
from lab.ontology.profile import fingerprint, prompt, uri
from lab.ontology.service import SemanticService
from lab.sources import SourceStore
from evidence_helpers import verification_reply

TEXT = '苏轼于元丰五年在黄州参加了一次文化活动。'
RESPONSE = {'entities': [
    {'id': 'a', 'label': '苏轼的文化活动', 'type': 'E7', 'concrete': True},
    {'id': 'b', 'label': '苏轼', 'type': 'E21'},
], 'statements': [{'subject': 'a', 'predicate': str(uri('P14')), 'object': 'b',
                  'quote': TEXT, 'start': 0}], 'deferred': []}


def fixed_tools(monkeypatch):
    def search(query, limit):
        return [{'title': title, 'url': 'https://example.org/' + path} for title, path in
                [('失败页面', 'bad'), ('合成测试资料', 'one'), ('重复正文', 'two')]]
    def fetch(url, title):
        if url.endswith('bad'):
            raise ValueError('unreadable')
        return {'text': TEXT, 'title': title, 'url': url, 'original_chars': len(TEXT)}
    async def request(self, cfg, messages):
        verification = verification_reply(messages)
        if verification is not None:
            return verification, {}
        output = ['宋朝文化'] if '网页检索词' in messages[-1]['content'] else RESPONSE
        return json.dumps(output, ensure_ascii=False), {}
    monkeypatch.setattr(demo, 'search_web', search)
    monkeypatch.setattr(demo, 'fetch_source', fetch)
    monkeypatch.setattr(ExtractionLLM, '_request', request)


def test_demo_partial_collection_deduplicates_and_publishes(monkeypatch, tmp_path):
    fixed_tools(monkeypatch)
    llm = ExtractionLLM(tmp_path / 'llm.json', lambda *args: None)
    llm.settings = LLMSettings(model='fixed-response')
    semantic = SemanticService(tmp_path / 'ontology')
    events = []
    original_hash = fingerprint()
    assert '文化实践' in prompt(TEXT, 'culture')
    result = asyncio.run(demo.build_demo('宋朝文化', 2, llm, SourceStore(tmp_path / 'sources'), semantic,
                                        lambda event, data: events.append(event)))
    assert result['collected'] == 1
    assert result['status'] == 'partial'
    assert result['published'] == 1
    assert result['failures'][0]['stage'] == 'fetch'
    claim = semantic.get_published_graph()['statements'][0]
    assert claim['evidence']['quote'] == TEXT
    assert claim['reviews'][0]['action'] == 'auto_publish'
    assert fingerprint() == original_hash
    assert '苏轼' in semantic.export('json')


def test_demo_page_runs_full_flow_and_exports(monkeypatch, tmp_path):
    fixed_tools(monkeypatch)
    monkeypatch.setenv('LAB_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('LLM_MODEL', raising=False)
    llm = ExtractionLLM(tmp_path / 'ontology-demo' / 'llm-settings.json', lambda *args: None)
    llm.save(LLMSettings(model='fixed-response'))
    app = AppTest.from_file(str(ROOT / 'demo.py')).run(timeout=30)
    assert not app.exception
    next(b for b in app.button if b.label == '一键搜集并构建图谱').click()
    app.run(timeout=60)
    assert not app.exception
    assert app.session_state['demo_result']['published'] == 1
    assert len(app.get('plotly_chart')) == 1
    assert len(app.get('download_button')) == 3


def test_planning_failure_falls_back_and_truncates(monkeypatch, tmp_path):
    queries = []
    monkeypatch.setattr(demo, 'search_web', lambda query, limit: queries.append(query) or [{'title': '长文', 'url': 'https://example.org/long'}])
    monkeypatch.setattr(demo, 'fetch_source', lambda *args: {'text': '甲' * 10000, 'title': '长文', 'url': args[0]})
    async def response(self, cfg, messages):
        if '网页检索词' in messages[-1]['content']:
            raise TimeoutError('SECRET')
        return '{"entities":[],"statements":[],"deferred":[]}', {}
    monkeypatch.setattr(ExtractionLLM, '_request', response)
    llm = ExtractionLLM(tmp_path / 'llm.json', lambda *a: None)
    llm.settings = LLMSettings(model='fixed')
    store = SourceStore(tmp_path / 'sources')
    result = asyncio.run(demo.build_demo('宋朝文化', 1, llm, store, SemanticService(tmp_path / 'ontology'), lambda *a: None))
    assert queries == ['宋朝文化']
    assert result['status'] == 'success' and result['extracted'] == 0
    source = store.list()[0]
    assert source['chars'] == 9000 and source['original_chars'] == 10000 and source['truncated']


def test_no_readable_sources_does_not_create_batch(monkeypatch, tmp_path):
    fixed_tools(monkeypatch)
    def fail(*args):
        raise ValueError('unreadable')
    monkeypatch.setattr(demo, 'fetch_source', fail)
    llm = ExtractionLLM(tmp_path / 'llm.json', lambda *a: None)
    llm.settings = LLMSettings(model='fixed')
    service = SemanticService(tmp_path / 'ontology')
    result = asyncio.run(demo.build_demo('宋朝文化', 1, llm, SourceStore(tmp_path / 'sources'), service, lambda *a: None))
    assert result['collected'] == 0 and result['batch_id'] is None and result['status'] == 'failed'
    assert service.batches() == []
