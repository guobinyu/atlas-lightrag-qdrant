"""Culture decoding, annotation, derived index and demand/design integration with deterministic services."""
import asyncio
from copy import deepcopy
import json

import pytest

from lab.culture import prompts as p
from lab.culture.service import CultureService
from lab.culture.store import Store
from lab import demand_design
from lab.demands import check_facts, check_element_refs, mapping_markdown
from test_demands import demand, work, DemandModel, INPUT, start, BRIEF_FIELDS

TEXT = '牡丹、莲花等植物题材常见于丝绸装饰，在某些语境中分别联系富贵、清净等寓意。婚礼丝巾常以正红为地，饰以金色牡丹。'
OTHER = '宋代织锦中也可见到牡丹纹样，多与缠枝结构组合。'
MODEL = {'model': 'fixed'}
RESPONSE = {'elements': [
    {'id': 'e1', 'type': 'motif', 'label': '牡丹纹', 'description': '丝绸装饰常见花卉题材', 'basis': 'documented', 'themes': [], 'quote': '牡丹、莲花等植物题材常见于丝绸装饰'},
    {'id': 'e2', 'type': 'meaning', 'label': '富贵', 'description': '部分语境中的寓意', 'basis': 'documented', 'themes': ['婚恋', '不存在的主题'], 'quote': '在某些语境中分别联系富贵、清净等寓意'},
    {'id': 'e3', 'type': 'palette', 'label': '正红金色', 'description': 'HEX 为色名近似转写', 'basis': 'documented', 'themes': ['婚恋'],
     'colors': [{'name': '正红', 'hex': '#c8102e', 'role': 'main'}, {'name': '金色', 'hex': '#C9A227', 'role': 'accent'}], 'quote': '婚礼丝巾常以正红为地，饰以金色牡丹'},
    {'id': 'e4', 'type': 'scene', 'label': '婚礼', 'basis': 'documented', 'themes': ['婚恋'], 'quote': '婚礼丝巾'},
    {'id': 'e5', 'type': 'motif', 'label': '凭空纹', 'basis': 'documented', 'themes': [], 'quote': '这句话在原文里完全不存在，也无法近似定位。'}],
    'relations': [
    {'subject': 'e1', 'predicate': 'symbolizes', 'object': 'e2', 'quote': '在某些语境中分别联系富贵、清净等寓意'},
    {'subject': 'e1', 'predicate': 'pairs_with', 'object': 'e3', 'quote': '饰以金色牡丹'},
    {'subject': 'e3', 'predicate': 'suits_scene', 'object': 'e4', 'quote': '婚礼丝巾常以正红为地'},
    {'subject': 'e1', 'predicate': 'symbolizes', 'object': 'e4', 'quote': '婚礼丝巾'},
    {'subject': 'e5', 'predicate': 'symbolizes', 'object': 'e2', 'quote': '富贵'}]}
SECOND = {'elements': [{'id': 'a', 'type': 'motif', 'label': ' 牡丹纹', 'description': '', 'basis': 'documented', 'themes': ['节日纪念'], 'quote': '宋代织锦中也可见到牡丹纹样'}],
          'relations': []}


class FakeIndex:
    """In-memory stand-in for CultureIndex; scores by shared characters."""
    def __init__(self):
        self.points, self.resets, self.upserts = {}, 0, 0

    def count(self):
        return len(self.points)

    def reset(self):
        self.points, self.resets = {}, self.resets + 1

    async def upsert(self, items):
        self.upserts += len(items)
        for eid, text, payload in items:
            self.points[eid] = (text, payload)

    def delete(self, ids):
        for i in ids:
            self.points.pop(i, None)

    async def search(self, query, *, limit=8, kinds=None, themes=None, threshold=0.2):
        chars = set(query.replace(' ', ''))
        hits = []
        for eid, (text, payload) in self.points.items():
            if (kinds and payload['type'] not in kinds) or (themes and not set(themes) & set(payload['themes'])):
                continue
            score = sum(c in text for c in chars) / max(1, len(chars))
            if score >= threshold:
                hits.append((eid, score))
        return sorted(hits, key=lambda h: -h[1])[:limit]


class Decoder:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    async def decode_culture(self, text, themes):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def source(text=TEXT, sid='a' * 24, title='丝绸纹样示例'):
    return {'id': sid, 'text': text, 'title': title, 'url': ''}


def decoded(tmp_path):
    service = CultureService(tmp_path)
    batch = service.create_batch([source()], MODEL)
    state = asyncio.run(service.run(batch, Decoder(RESPONSE), lambda *a: None))
    assert state == 'success'
    return service, batch


def by_label(service, label, status='published'):
    return next(i for i in service.elements(status=status) if i['label'] == label)


def test_parse_rejects_structure_and_reports_soft_issues():
    with pytest.raises(ValueError, match='两个数组'):
        p.parse('{"elements": []}', p.THEMES)
    with pytest.raises(ValueError, match='引文'):
        p.parse(json.dumps({'elements': [{'id': 'x', 'type': 'motif', 'label': '纹'}], 'relations': []}), p.THEMES)
    bad = deepcopy(RESPONSE)
    bad['elements'][2]['colors'][0]['hex'] = '#12345'
    with pytest.raises(ValueError, match='HEX'):
        p.parse(json.dumps(bad), p.THEMES)
    bad = deepcopy(RESPONSE)
    bad['elements'][0]['colors'] = [{'name': '红', 'hex': '#FF0000', 'role': 'main'}]
    with pytest.raises(ValueError, match='只有配色'):
        p.parse(json.dumps(bad), p.THEMES)
    elements, relations, issues = p.parse(json.dumps(RESPONSE), p.THEMES)
    assert elements['e2']['basis'] == 'interpretation'
    assert elements['e2']['themes'] == ['婚恋']
    assert elements['e3']['colors'][0]['hex'] == '#C8102E'
    assert len(relations) == 4
    assert any('不存在的主题' in i for i in issues)
    assert any('e1 symbolizes e4' in i for i in issues)


def test_decode_stores_only_located_quotes_with_relations(tmp_path):
    service, batch = decoded(tmp_path)
    labels = {i['label'] for i in service.elements()}
    assert labels == {'牡丹纹', '富贵', '正红金色', '婚礼'}
    motif = by_label(service, '牡丹纹')
    assert {(r['label'], r['other']) for r in motif['relations'] if r['direction'] == 'out'} == {('象征', '富贵'), ('搭配配色', '正红金色')}
    assert motif['evidence'][0]['quote'] in TEXT and motif['evidence'][0]['match_method'] == 'exact'
    assert by_label(service, '富贵')['basis'] == 'interpretation'
    summary = service.summary(batch)
    assert summary == {'batch_id': batch, 'blocks': 1, 'failed_blocks': 0, 'elements': 4, 'relations': 3, 'issues': 4}
    issues = service.report(batch)[0]['issues']
    assert any('凭空纹' in i and '无法在原文定位' in i for i in issues)
    assert any('端点素材未入库' in i for i in issues)
    # A successful block is immutable; replaying the response adds nothing.
    assert service.save_response(service.report(batch)[0]['id'], json.dumps(RESPONSE)) == {'elements': 0, 'relations': 0, 'issues': 0}
    assert len(service.elements()) == 4


def test_same_type_and_name_merge_keeps_every_sources_evidence(tmp_path):
    service, _ = decoded(tmp_path)
    batch = service.create_batch([source(OTHER, 'b' * 24, '宋锦示例')], MODEL)
    asyncio.run(service.run(batch, Decoder(SECOND), lambda *a: None))
    motif = by_label(service, '牡丹纹')
    assert len([i for i in service.elements() if i['type'] == 'motif']) == 1
    assert [e['sample'] for e in motif['evidence']] == ['丝绸纹样示例', '宋锦示例']
    assert motif['themes'] == ['节日纪念']
    assert motif['description'] == '丝绸装饰常见花卉题材'
    assert {s['title']: s['elements'] for s in service.samples()} == {'丝绸纹样示例': 4, '宋锦示例': 1}
    assert [i['label'] for i in service.elements(sample_id='b' * 24)] == ['牡丹纹']


def test_failed_block_retry_and_restart_recovery(tmp_path):
    service = CultureService(tmp_path)
    batch = service.create_batch([source()], MODEL)
    decoder = Decoder(ValueError('模型服务返回 HTTP 500，secret-body'), '{invalid', RESPONSE)
    assert asyncio.run(service.run(batch, decoder, lambda *a: None)) == 'failed'
    error = service.report(batch)[0]['error']
    assert 'HTTP 500' in error and 'secret-body' not in error
    with pytest.raises(ValueError, match='模型或解码版本'):
        service.prepare_retry(batch, [source()], {'model': 'other'})
    with pytest.raises(ValueError, match='正文已变化'):
        service.prepare_retry(batch, [source(TEXT + '。')], MODEL)
    service.prepare_retry(batch, [source()], MODEL)
    assert asyncio.run(service.run(batch, decoder, lambda *a: None)) == 'failed'
    assert 'JSON 协议解析' in service.report(batch)[0]['error']
    service.prepare_retry(batch, [source()], MODEL)
    assert asyncio.run(service.run(batch, decoder, lambda *a: None)) == 'success'
    assert decoder.calls == 3
    with pytest.raises(ValueError, match='没有可重试'):
        service.prepare_retry(batch, [source()], MODEL)
    other = service.create_batch([source(OTHER, 'b' * 24)], MODEL)
    assert CultureService(tmp_path).batch(other)['status'] == 'interrupted'
    assert CultureService(tmp_path).report(other)[0]['status'] == 'pending'


def test_manual_annotation_relations_and_theme_rules(tmp_path):
    service, _ = decoded(tmp_path)
    eid = service.annotate({'type': 'motif', 'label': '缠枝纹', 'description': '连续卷草结构', 'basis': 'documented', 'themes': ['婚恋']})
    item = service.detail(eid)
    assert item['origin'] == 'manual' and item['evidence'] == []
    with pytest.raises(ValueError, match='同名'):
        service.annotate({'type': 'motif', 'label': '缠 枝纹'})
    with pytest.raises(ValueError, match='未知主题'):
        service.annotate({'type': 'motif', 'label': '回纹', 'themes': ['不存在']})
    with pytest.raises(ValueError, match='颜色'):
        service.annotate({'type': 'palette', 'label': '空配色', 'colors': []})
    meaning = service.annotate({'type': 'meaning', 'label': '绵延', 'basis': 'documented'})
    assert service.detail(meaning)['basis'] == 'interpretation'
    service.relate(eid, 'symbolizes', meaning)
    with pytest.raises(ValueError, match='类型约束'):
        service.relate(meaning, 'symbolizes', eid)
    with pytest.raises(ValueError, match='不能修改类型'):
        service.annotate({'type': 'ip', 'label': '缠枝纹'}, eid)
    motif = by_label(service, '牡丹纹')
    service.annotate({'type': 'motif', 'label': '牡丹纹', 'description': '人工修订描述', 'basis': 'documented', 'themes': ['婚恋']}, motif['id'])
    edited = service.detail(motif['id'])
    assert edited['description'] == '人工修订描述' and len(edited['evidence']) == 1 and edited['origin'] == 'extracted'
    service.add_theme('毕业季')
    assert service.themes()[-1] == '毕业季'
    with pytest.raises(ValueError, match='已存在'):
        service.add_theme('婚恋')
    assert [i['label'] for i in service.elements(theme='未分主题')] == ['绵延']
    service.set_status(motif['id'], 'rejected')
    assert motif['id'] not in {i['id'] for i in service.elements()}
    assert service.detail(motif['id'])['status'] == 'rejected'


def test_derived_index_sync_is_incremental_and_rebuilds_after_divergence(tmp_path):
    service, _ = decoded(tmp_path)
    index = FakeIndex()
    assert asyncio.run(service.sync(index)) == {'indexed': 4, 'updated': 4, 'removed': 0}
    assert asyncio.run(service.sync(index))['updated'] == 0
    motif = by_label(service, '牡丹纹')
    assert '原文：牡丹、莲花' in index.points[motif['id']][0]
    service.annotate({'type': 'motif', 'label': '牡丹纹', 'description': '新描述', 'basis': 'documented'}, motif['id'])
    assert asyncio.run(service.sync(index))['updated'] == 1
    service.set_status(motif['id'], 'rejected')
    assert asyncio.run(service.sync(index)) == {'indexed': 3, 'updated': 0, 'removed': 1}
    index.points.clear()
    assert asyncio.run(service.sync(index))['updated'] == 3
    assert index.resets == 1 and index.count() == 3


def test_context_prefers_hinted_theme_and_cites_only_quotes(tmp_path):
    service, _ = decoded(tmp_path)
    index = FakeIndex()
    entries = asyncio.run(service.context(index, '牡丹 婚礼 正红', '为新人设计婚礼回礼'))
    assert {e['label'] for e in entries[:3]} == {'富贵', '正红金色', '婚礼'}
    assert entries[-1]['label'] == '牡丹纹'
    palette = next(e for e in entries if e['type'] == 'palette')
    assert palette['colors'][0]['hex'] == '#C8102E'
    assert palette['text'] == '婚礼丝巾常以正红为地，饰以金色牡丹' and '配色' in palette['card']
    assert [item['label'] for item, _ in asyncio.run(service.search(index, '牡丹', kinds=['motif']))] == ['牡丹纹']
    evidence = {'M' + str(i): e for i, e in enumerate(entries, 1)}
    meaning = next(k for k, e in evidence.items() if e['basis'] == 'interpretation')
    motif = next(k for k, e in evidence.items() if e['type'] == 'motif')
    with pytest.raises(ValueError, match='设计解读'):
        check_facts({'facts': [{'text': '寓意', 'citations': [{'ref': meaning, 'quote': '联系富贵'}]}]}, evidence)
    check_facts({'facts': [{'text': '常见题材', 'citations': [{'ref': motif, 'quote': '植物题材常见于丝绸装饰'}]}]}, evidence)
    with pytest.raises(ValueError, match='无法定位'):
        check_facts({'facts': [{'text': '摘要不是原文', 'citations': [{'ref': motif, 'quote': '丝绸装饰常见花卉题材'}]}]}, evidence)
    check_element_refs([{'element_ref': motif}, {}], evidence)
    with pytest.raises(ValueError, match='M 开头'):
        check_element_refs([{'element_ref': 'K1'}], {'K1': {'content': '片段'}})


def test_export_json_and_jsonl_contain_published_elements_with_quotes(tmp_path):
    service, _ = decoded(tmp_path)
    service.set_status(by_label(service, '婚礼')['id'], 'rejected')
    bundle = json.loads(service.export('json'))
    assert bundle['metadata']['decode_prompt'] == p.VERSION
    assert {e['label'] for e in bundle['elements']} == {'牡丹纹', '富贵', '正红金色'}
    assert bundle['samples'][0]['title'] == '丝绸纹样示例'
    lines = [json.loads(line) for line in service.export('jsonl').splitlines()]
    assert len(lines) == 3 and all(line['evidence'] for line in lines)
    with pytest.raises(ValueError):
        service.export('csv')


def test_newer_schema_is_refused(tmp_path):
    Store(tmp_path / 'culture.sqlite3')
    import sqlite3
    db = sqlite3.connect(tmp_path / 'culture.sqlite3')
    db.execute('PRAGMA user_version=2')
    db.commit()
    db.close()
    with pytest.raises(ValueError, match='版本较新'):
        CultureService(tmp_path)


class RefModel(DemandModel):
    ref = None

    async def __call__(self, prompt, **kwargs):
        raw = await super().__call__(prompt, **kwargs)
        value = json.loads(raw)
        if prompt.startswith('DEMAND_CANDIDATES') and self.ref:
            for candidate in value['candidates']:
                candidate['story_mapping'][0]['element_ref'] = self.ref
        return json.dumps(value, ensure_ascii=False)


def culture_demand(demand, tmp_path):
    service, _ = decoded(tmp_path / 'culture')
    entries = asyncio.run(service.context(FakeIndex(), '牡丹 婚礼 正红', '婚礼'))
    async def context(question, hint=''):
        return deepcopy(entries)
    demand.engine.culture_context = context
    demand.llm = RefModel(demand.llm)
    return {'M' + str(i): e for i, e in enumerate(entries, 1)}


def test_demand_chain_freezes_elements_and_validates_refs(demand, tmp_path):
    evidence = culture_demand(demand, tmp_path)
    motif = next(k for k, e in evidence.items() if e['type'] == 'motif')
    palette = next(k for k, e in evidence.items() if e['type'] == 'palette')
    demand.llm.ref = 'M9'
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'failed'
    assert 'element_ref' in list(run['operations'].values())[-1]['responses'][-1]['error']
    demand.llm.ref = motif
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'success'
    batch = run['candidate_rounds'][-1]
    assert batch['context']['culture_missing'] is False
    assert batch['context']['evidence'][motif]['label'] == '牡丹纹'
    assert batch['candidates'][0]['story_mapping'][0]['element_ref'] == motif
    assert f'文化素材 {motif}' in mapping_markdown(batch['candidates'][0]['story_mapping'])
    demand.demands.select_candidate(run['id'], batch['id'], 'C1')
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    brief = run['briefs'][0]
    assert brief['culture_missing'] is False and brief['evidence'] == batch['context']['evidence']
    run, status = start(demand, run, 'design_generate', brief_id=brief['id'])
    assert status['status'] == 'success'
    from design_fixture import proposal
    value = proposal(demand_design.model_input(brief, None, ''))
    value['palette'][0]['element_ref'] = palette
    with pytest.raises(ValueError, match='取自该素材'):
        demand_design.validate(value, brief)
    value['palette'][0]['hex'] = '#C8102E'
    for app in value['applications']:
        app['palette_refs'] = ['#C8102E']
    demand_design.validate(value, brief)
    value['story_mapping'][0]['element_ref'] = 'M99'
    with pytest.raises(ValueError, match='M 开头'):
        demand_design.validate(value, brief)


def test_prompts_explain_element_refs_and_empty_library_is_reported(demand):
    from lab.demands import CANDIDATE_V3, BRIEF_V3
    assert 'element_ref' in CANDIDATE_V3 and 'element_ref' in BRIEF_V3 and 'element_ref' in demand_design.PROMPT
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'success'
    assert run['candidate_rounds'][0]['context']['culture_missing'] is True
    assert not any(k.startswith('M') for k in run['candidate_rounds'][0]['context']['evidence'])
