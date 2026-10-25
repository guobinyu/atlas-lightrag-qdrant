"""Versioned concept-design outputs, immutable history and exploration context."""
from copy import deepcopy
import json

import pytest

from test_demands import demand, work, INPUT, selected, start, candidates
from lab.demands import export_bundle, export_markdown, brief_sections, check_followups, CONTENT_FORMAT_VERSION


def test_confirm_binds_exploration_and_upstream_change_drops_it(demand):
    service = demand.demands
    run, _ = start(demand, service.create(INPUT), 'analyze')
    run['clarifications'][-1]['suggestions'] = {'theme': '重逢纪念'}
    service.save(run)
    session = service.clarification_session(run['id'], run['clarifications'][-1]['id'])
    service.save_answer(run['id'], session['id'], 0, unknown=True)
    current = service.confirm(run['id'], INPUT, 1)
    snapshot = current['summaries'][-1]['exploration']
    assert snapshot['source_revision'] == 1
    assert snapshot['source_analysis_id'] == run['clarifications'][-1]['id']
    assert '价格未定' in snapshot['unknown']
    assert '面向哪个价格区间？' in snapshot['unknown']
    assert snapshot['suggestions']['theme'] == '重逢纪念'
    assert current['inputs']['theme'] == ''
    current, status = start(demand, current, 'candidates')
    assert status['status'] == 'success'
    assert current['candidate_rounds'][-1]['context']['summary']['exploration'] == snapshot
    old = deepcopy(current['candidate_rounds'][-1])
    current = service.update(run['id'], {**INPUT, 'audience': '游客'})
    current = service.confirm(run['id'], current['inputs'], current['revision'])
    assert current['summaries'][-1]['exploration']['source_analysis_id'] is None
    assert current['summaries'][-1]['exploration']['suggestions'] == {}
    assert current['candidate_rounds'][-1] == old


def test_latest_exports_complete_auxiliary_content_and_revisions(demand):
    run = selected(demand)
    assert run['candidate_rounds'][0]['format_version'] == CONTENT_FORMAT_VERSION
    assert run['candidate_rounds'][0]['candidates'][0]['story']
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    original = deepcopy(run['briefs'][0])
    assert original['format_version'] == CONTENT_FORMAT_VERSION
    md = export_markdown(run, original)
    expected = ['推荐产品方案', '文化主题与故事', '具体设计建议', '设计交付要求', '商家能力与硬性限制', '必要假设与验证', '后续确认与交接安排', '参考资料与来源']
    assert [md.index('## ' + label) for label in expected] == sorted(md.index('## ' + label) for label in expected)
    assert '商家' in md and '包装定稿前' in md and '概念稿先保留位置' in md
    assert export_bundle(run, original)['schema_version'] == CONTENT_FORMAT_VERSION
    assert export_bundle(run, original)['brief'] == original
    assert [title for title, _, aux in brief_sections(run, original) if aux][:2] == ['必要假设与验证', '后续确认与交接安排']
    run, status = start(demand, run, 'revise', brief_id=original['id'], instruction='简化视觉元素')
    assert status['status'] == 'success'
    assert run['briefs'][0] == original


def test_legacy_retry_and_upgrade_keep_original(demand):
    service = demand.demands
    run = selected(demand)
    batch = run['candidate_rounds'][0]
    batch.pop('format_version')
    for c in batch['candidates']:
        c.pop('story')
        c.pop('visual_elements')
    service.save(run)
    demand.llm.bad = 'DEMAND_BRIEF'
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    op.pop('format_version')  # Saved before this upgrade.
    service.save(run)
    demand.llm.bad = None
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    original = deepcopy(run['briefs'][0])
    assert original['format_version'] == 1
    assert 'questions' in original['content'] and 'followups' not in original['content']
    before = export_markdown(run, original)
    assert '## 待确认事项' in before
    assert export_bundle(run, original)['schema_version'] == 1
    run, status = start(demand, run, 'revise', brief_id=original['id'], instruction='整理成概念设计交接建议')
    assert status['status'] == 'success'
    assert run['briefs'][-1]['format_version'] == CONTENT_FORMAT_VERSION
    assert run['briefs'][0] == original
    assert export_markdown(run, original) == before


@pytest.mark.parametrize('value', [ {}, {'followups': ['谁来确认？']}, {'followups': [{'item': '参数', 'category': '错误', 'owner': '商家', 'timing': '定稿前', 'approach': '预留位置'}]}, {'followups': [], 'questions': ['谁来确认？']}])
def test_reject_unstructured_handoff(value):
    with pytest.raises(ValueError):
        check_followups(value)


@pytest.mark.parametrize('problem', ['story', 'visual_elements', 'duplicate'])
def test_invalid_candidate_output_not_published(demand, problem):
    previous = demand.llm
    class InvalidModel:
        settings, configured = previous.settings, True
        async def __call__(self, prompt, **kwargs):
            value = json.loads(await previous(prompt, **kwargs))
            if prompt.startswith('DEMAND_CANDIDATES'):
                if problem == 'duplicate':
                    value['candidates'][1] = {**value['candidates'][0], 'name': '仅换名字'}
                else:
                    value['candidates'][0].pop(problem)
            return json.dumps(value)
    demand.llm = InvalidModel()
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'failed'
    assert not run['candidate_rounds']
    assert list(run['operations'].values())[-1]['responses'][-1]['error']
