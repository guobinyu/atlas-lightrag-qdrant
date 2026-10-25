"""Comparable products, traceable concept briefs and v1/v2 compatibility."""
from copy import deepcopy
import json

import pytest

from test_demands import demand, work, INPUT, TEXT, start, candidates, selected
from test_demand_ui import page
from lab.demands import export_bundle, export_markdown, candidate_comparison


@pytest.fixture(autouse=True)
def content_v3(monkeypatch):
    # These tests cover the v3 format; v4 has its own tests.
    monkeypatch.setattr('lab.demands.CONTENT_FORMAT_VERSION', 3)


def test_v3_comparison_selection_and_design_handoff(demand):
    inputs = {**INPUT, 'purpose': '聚会后带走纪念'}
    run = candidates(demand, inputs)
    batch = run['candidate_rounds'][0]
    assert batch['format_version'] == 3
    rows = candidate_comparison(batch)
    assert len(rows) == 3
    assert all(row['核心用途'] == inputs['purpose'] for row in rows)
    assert len({row['区别'] for row in rows}) == 3
    assert all(row['需确认能力'] == '新盒型需确认' for row in rows)
    calls = len(demand.llm.calls)
    demand.demands.select_candidate(run['id'], batch['id'], 'C1')
    assert len(demand.llm.calls) == calls
    assert demand.demands.load(run['id'])['inputs'] == inputs
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    data = demand.llm.inputs[-1]['decision_context']
    assert data['confirmed_conditions']['inputs'] == inputs
    assert data['selected_direction']['name'] == batch['candidates'][0]['name']
    assert data['proposed_details']['capability_gaps'] == ['新盒型需确认']
    brief = deepcopy(run['briefs'][0])
    assert brief['candidate'] == batch['candidates'][0]
    assert brief['format_version'] == 3
    md = export_markdown(run, brief)
    for text in ('故事到设计的对应', '表达共同记忆', '核心交付', '可选交付', '瓶身标签概念稿', '留言卡',
                 '推进顺序与等待条件', '后续确认与交接安排', '品牌文件与参数文案'):
        assert text in md
    assert export_bundle(run, brief)['schema_version'] == 3
    assert export_bundle(run, brief)['brief']['content']['deliverables'] == brief['content']['deliverables']
    run, status = start(demand, run, 'revise', brief_id=brief['id'], instruction='保留主线，简化装饰')
    assert status['status'] == 'success'
    assert run['briefs'][0] == brief
    assert demand.llm.inputs[-1]['base'] == brief
    assert run['candidate_rounds'][0] == batch


@pytest.mark.parametrize('version', [1, 2])
def test_old_brief_retry_and_new_revision_preserve_original(demand, monkeypatch, version):
    monkeypatch.setattr('lab.demands.CONTENT_FORMAT_VERSION', version)
    run = selected(demand)
    old_batch = deepcopy(run['candidate_rounds'][0])
    assert 'story_mapping' not in old_batch['candidates'][0]
    demand.llm.bad = 'DEMAND_BRIEF'
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    monkeypatch.setattr('lab.demands.CONTENT_FORMAT_VERSION', 3)
    demand.llm.bad = None
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    old = deepcopy(run['briefs'][0])
    before = export_markdown(run, old)
    assert old['format_version'] == version
    assert 'story_mapping' not in old['content']
    assert 'deliverables' not in old['content']
    run, status = start(demand, run, 'revise', brief_id=old['id'], instruction='明确概念稿交付')
    assert status['status'] == 'success'
    assert run['briefs'][-1]['format_version'] == 3
    assert run['briefs'][-1]['content']['story_mapping']
    assert run['briefs'][0] == old
    assert export_markdown(run, old) == before
    assert run['candidate_rounds'][0] == old_batch
    # A new brief can also start from the same unchanged old candidate.
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    assert run['briefs'][-1]['format_version'] == 3


@pytest.mark.parametrize('version', [1, 2])
def test_old_failed_candidate_retries_with_original_schema(demand, monkeypatch, version):
    monkeypatch.setattr('lab.demands.CONTENT_FORMAT_VERSION', version)
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    demand.llm.bad = 'DEMAND_CANDIDATES'
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    monkeypatch.setattr('lab.demands.CONTENT_FORMAT_VERSION', 3)
    demand.llm.bad = None
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    assert run['candidate_rounds'][0]['format_version'] == version
    assert 'purpose' not in run['candidate_rounds'][0]['candidates'][0]


@pytest.mark.parametrize('kind,problem', [('candidates', 'purpose'), ('candidates', 'differentiator'),
    ('candidates', 'capability_gaps'), ('candidates', 'empty_mapping'), ('candidates', 'incomplete_mapping'),
    ('brief', 'empty_mapping'), ('brief', 'empty_deliverables'), ('brief', 'no_core'),
    ('brief', 'bad_priority'), ('brief', 'missing_description')])
def test_invalid_v3_output_is_not_published_and_can_retry(demand, kind, problem):
    run = selected(demand) if kind == 'brief' else demand.demands.create(INPUT)
    if kind == 'candidates':
        demand.demands.approve(run['id'])
    previous = demand.llm
    class InvalidModel:
        settings, configured = previous.settings, True
        async def __call__(self, prompt, **kwargs):
            result = json.loads(await previous(prompt, **kwargs))
            if not prompt.startswith('DEMAND_' + kind.upper() + '：'):
                return json.dumps(result)
            item = result['candidates'][0] if kind == 'candidates' else result
            if problem == 'empty_mapping':
                item['story_mapping'] = []
            elif problem == 'incomplete_mapping':
                item['story_mapping'][0].pop('placement')
            elif problem == 'empty_deliverables':
                item['deliverables'] = []
            elif problem == 'no_core':
                for d in item['deliverables']:
                    d['priority'] = 'optional'
            elif problem == 'bad_priority':
                item['deliverables'][0]['priority'] = 'required'
            elif problem == 'missing_description':
                item['deliverables'][0].pop('description')
            else:
                item.pop(problem)
            return json.dumps(result)
    demand.llm = InvalidModel()
    run, status = start(demand, run, kind)
    assert status['status'] == 'failed'
    assert not run['briefs']
    if kind == 'candidates':
        assert not run['candidate_rounds']
    op = list(run['operations'].values())[-1]
    assert op['responses'][-1]['error']
    demand.llm = previous
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'


def test_v3_local_evidence_and_historical_unknowns_are_preserved(demand):
    async def context(query):
        return [{'text': TEXT, 'title': '织造展览资料'}]
    demand.engine.campaign_context = context
    run = demand.demands.create(INPUT)
    run['summaries'][0]['exploration'] = {'unknown': ['单瓶还是双瓶？'], 'suggestions': {}}
    demand.demands.save(run)
    demand.demands.approve(run['id'])
    run, _ = start(demand, run, 'candidates')
    batch = run['candidate_rounds'][0]
    assert batch['context']['evidence']['K1']['text'] == TEXT
    assert not batch['context']['knowledge_missing']
    demand.demands.select_candidate(run['id'], batch['id'], 'C1')
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    assert demand.llm.inputs[-1]['decision_context']['historical_exploration']['unknown'] == ['单瓶还是双瓶？']
    md = export_markdown(run, run['briefs'][0])
    assert '历史需求探索记录' in md and '单瓶还是双瓶？' in md
    assert '当前待办以后续确认与交接安排为准' in md


def test_v3_ui_comparison_mapping_and_deliverables(demand):
    run = selected(demand)
    run, _ = start(demand, run, 'brief')
    calls = len(demand.llm.calls)
    app = page(demand)
    assert not app.exception
    assert len(app.table) == 1
    assert list(app.table[0].value.columns) == ['产品方向', '核心用途', '主要体验', '区别', '需确认能力']
    assert any('瓶身标签概念稿' in m.value for m in app.markdown)
    assert any('核心交付' in m.value for m in app.markdown)
    assert len([e for e in app.expander if e.label == '故事到设计的对应（策划建议）']) == 3
    assert len(demand.llm.calls) == calls
