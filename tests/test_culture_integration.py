"""Culture decoding through the real Engine, local Qdrant and bge-small-zh; model replies are fixed."""
import asyncio
import json

from lab.config import ROOT, Settings
from lab.engine import Engine

TEXT = (ROOT / 'examples/05-丝绸纹样与服饰.md').read_text(encoding='utf-8')
RESPONSE = {'elements': [
    {'id': 'e1', 'type': 'motif', 'label': '牡丹纹', 'description': '丝绸装饰中的花卉题材', 'basis': 'documented', 'themes': ['婚恋'], 'quote': '牡丹、莲花等植物题材常见于丝绸装饰'},
    {'id': 'e2', 'type': 'meaning', 'label': '富贵', 'description': '部分语境中的寓意', 'themes': ['婚恋'], 'quote': '在某些语境中分别联系富贵、清净等寓意'},
    {'id': 'e3', 'type': 'motif', 'label': '联珠纹', 'description': '连续珠点形成装饰带', 'basis': 'documented', 'themes': [], 'quote': '联珠纹以连续珠点形成装饰带或轮廓'},
    {'id': 'e4', 'type': 'category', 'label': '丝绸服饰', 'description': '由面料组织、裁剪等共同决定外观', 'basis': 'documented', 'themes': [], 'quote': '丝绸服饰的外观由面料组织、裁剪、缝制、衬里和装饰共同决定'}],
    'relations': [{'subject': 'e1', 'predicate': 'symbolizes', 'object': 'e2', 'quote': '在某些语境中分别联系富贵、清净等寓意'},
                  {'subject': 'e3', 'predicate': 'fits_category', 'object': 'e4', 'quote': '联珠纹以连续珠点形成装饰带或轮廓'}]}


class Decoder:
    def __init__(self, settings):
        self.settings, self.configured, self.calls = settings, True, 0

    async def decode_culture(self, text, themes):
        self.calls += 1
        # Every block gets the same reply; quotes outside a block are reported, not stored.
        return json.dumps(RESPONSE, ensure_ascii=False)


def test_decode_index_filter_and_restart(tmp_path):
    (tmp_path / 'models').symlink_to(ROOT / 'data/models', target_is_directory=True)
    settings = Settings(data_dir=tmp_path, workspace='culture_fixture', seed_demo=False)
    engine = Engine(settings)
    try:
        engine.ready.result(timeout=120)
        work = engine.workbench
        work.llm.settings.model = 'deterministic-culture'
        work.llm = Decoder(work.llm.settings)
        source = work.sources.add({'title': '丝绸纹样与服饰', 'text': TEXT, 'url': ''})
        result = work.start('culture_decode', {'source_ids': [source['id']]})[0].result(timeout=60)
        assert result['status'] == 'success', result.get('message')
        assert result['culture_result']['elements'] == 4
        assert result['culture_result']['index']['indexed'] == 4
        assert not engine.gate.locked()
        assert engine.culture_index.count() == 4
        hits = engine.culture_search('婚礼 牡丹 吉祥寓意', themes=['婚恋']).result(timeout=60)
        assert {item['label'] for item, _ in hits} <= {'牡丹纹', '富贵'} and hits
        assert hits[0][1] > 0.2
        motifs = engine.culture_search('珠点装饰带', kinds=['motif']).result(timeout=60)
        assert motifs[0][0]['label'] == '联珠纹'
        # Task-internal context runs on the engine loop without a nested gate.
        entries = asyncio.run_coroutine_threadsafe(engine.culture_context('婚礼丝巾 牡丹', '婚礼回礼'), engine.loop).result(timeout=60)
        assert entries[0]['themes'] == ['婚恋'] and entries[0]['text'] in TEXT
        # LightRAG retrieval and its projection are untouched.
        assert engine.info()['total_points'] == 0
        assert work.semantic.get_published_graph()['statements'] == []
    finally:
        engine.close()
    engine = Engine(settings)
    try:
        engine.ready.result(timeout=120)
        assert engine.culture_index.count() == 4
        assert len(engine.workbench.culture.elements()) == 4
    finally:
        engine.close()
