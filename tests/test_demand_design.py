"""Frozen-source proposals, validation, recovery and page integration."""
from copy import deepcopy
import json

import pytest

from test_demands import demand, work, INPUT, TEXT, start, selected
from test_demand_ui import page, button, wait
from lab.demands import DemandService
from lab.demand_design import export_bundle, export_markdown


def briefed(demand):
    run = selected(demand)
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    return run


def designed(demand):
    run = briefed(demand)
    run, status = start(demand, run, 'design_generate', brief_id=run['briefs'][0]['id'])
    assert status['status'] == 'success'
    return run


def test_generate_freezes_source_without_new_retrieval_and_revision_is_immutable(demand, monkeypatch):
    run = briefed(demand)
    before = deepcopy(run)
    async def forbidden(*a, **k):
        raise AssertionError('设计阶段不能重新检索资料')
    monkeypatch.setattr(demand.engine, 'campaign_context', forbidden)
    monkeypatch.setattr('lab.demands.search_web', forbidden)
    run, status = start(demand, run, 'design_generate', brief_id=run['briefs'][0]['id'])
    assert status['status'] == 'success'
    v1 = deepcopy(run['design_versions'][0])
    assert v1['content_version'] == 1
    assert v1['brief_snapshot'] == before['briefs'][0]
    assert run['briefs'] == before['briefs'] and run['inputs'] == INPUT
    assert demand.llm.inputs[-1]['confirmed_conditions'] == before['briefs'][0]['summary']
    bundle = export_bundle(run, v1)
    assert bundle['design'] == v1
    bundle['design']['content']['title'] = '不能改写原记录'
    assert v1['content']['title'] != bundle['design']['content']['title']
    md = export_markdown(run, v1)
    for expected in ('整体概念', '视觉设计', '产品应用与交付', '#F3EEE4', '重逢，留一行给今天', '瓶身标签概念稿', '后续确认', '来源与依据'):
        assert expected in md
    run, status = start(demand, run, 'design_revise', design_id=v1['id'], instruction='主色改为墨绿，其他主线保留')
    assert status['status'] == 'success'
    v2 = run['design_versions'][1]
    assert v2['version'] == 2 and v2['base_id'] == v1['id']
    assert v2['content']['palette'][0]['hex'] == '#173F36'
    assert all('#173F36' in a['palette_refs'] for a in v2['content']['applications'])
    assert run['design_versions'][0] == v1
    assert run['briefs'] == before['briefs']
    assert demand.llm.calls[-2:] == ['DEMAND_DESIGN', 'DEMAND_DESIGN']


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_brief_can_generate_design_without_mutation(demand, monkeypatch, version):
    monkeypatch.setattr('lab.demands.CONTENT_FORMAT_VERSION', version)
    run = briefed(demand)
    original = deepcopy(run['briefs'][0])
    assert 'deliverables' not in original['content']
    run, status = start(demand, run, 'design_generate', brief_id=original['id'])
    assert status['status'] == 'success'
    design = run['design_versions'][0]
    assert design['content']['applications'][0]['deliverable_id'] == ''
    assert any('补充建议' in a for a in design['content']['assumptions'])
    assert run['briefs'][0] == original
    assert DemandService(demand).load(run['id'])['design_versions'][0] == design


@pytest.mark.parametrize('problem', ['bad_hex', 'missing_role', 'unknown_color', 'empty_app', 'missing_core', 'optional_to_core', 'wrong_id', 'missing_layout', 'empty_copy', 'bad_fact'])
def test_invalid_design_is_rejected_and_retries_saved_source(demand, problem):
    run = briefed(demand)
    run['briefs'][0]['content']['deliverables'].append({'name': '盒面', 'priority': 'core', 'description': '桂花与留白'})
    if problem == 'bad_fact':
        run['briefs'][0]['evidence'] = {'K1': {'text': TEXT}}
    demand.demands.save(run)
    previous = demand.llm
    class InvalidModel:
        settings, configured = previous.settings, True
        async def __call__(self, prompt, **kwargs):
            v = json.loads(await previous(prompt, **kwargs))
            if problem == 'bad_hex': v['palette'][0]['hex'] = '#GG0000'
            elif problem == 'missing_role': v['palette'] = v['palette'][:1]
            elif problem == 'unknown_color': v['applications'][0]['palette_refs'] = ['#FFFFFF']
            elif problem == 'empty_app': v['applications'] = []
            elif problem == 'missing_core': v['applications'] = v['applications'][:2]
            elif problem == 'optional_to_core': v['applications'][1]['priority'] = 'core'
            elif problem == 'wrong_id': v['applications'][0]['deliverable_id'] = 'D99'
            elif problem == 'missing_layout': v['applications'][0].pop('layout')
            elif problem == 'empty_copy': v['copywriting'] = []
            elif problem == 'bad_fact': v['facts'] = [{'text': '错误引用', 'citations': [{'ref': 'K1', 'quote': '原文没有这句话'}]}]
            return json.dumps(v)
    demand.llm = InvalidModel()
    source = deepcopy(run['briefs'][0])
    run, status = start(demand, run, 'design_generate', brief_id=source['id'])
    assert status['status'] == 'failed' and not run.get('design_versions')
    op = list(run['operations'].values())[-1]
    assert op['responses'][0]['error'] and op['brief_snapshot'] == source
    demand.llm = previous
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    assert run['design_versions'][0]['brief_snapshot'] == source
    assert 'previous_invalid_output' in demand.llm.inputs[-1]


def test_history_does_not_force_latest_brief_and_other_selections_are_rejected(demand):
    run = designed(demand)
    first = deepcopy(run['design_versions'][0])
    original_brief = run['briefs'][0]
    run, _ = start(demand, run, 'revise', brief_id=original_brief['id'], instruction='简化装饰')
    newer = run['briefs'][1]
    run, status = start(demand, run, 'design_generate', brief_id=newer['id'])
    assert status['status'] == 'success'
    run, status = start(demand, run, 'design_revise', design_id=first['id'], instruction='主色改为墨绿')
    assert status['status'] == 'success'
    assert run['design_versions'][-1]['brief_id'] == original_brief['id']
    assert run['design_versions'][0] == first
    batch = run['candidate_rounds'][0]
    demand.demands.select_candidate(run['id'], batch['id'], 'C2')
    with pytest.raises(ValueError, match='当前条件'):
        start(demand, run, 'design_generate', brief_id=original_brief['id'])
    with pytest.raises(ValueError, match='当前条件'):
        start(demand, run, 'design_revise', design_id=first['id'], instruction='墨绿')
    assert '重逢' in export_markdown(demand.demands.load(run['id']), first)


def test_restart_retry_duplicate_submission_and_upstream_invalidation(demand):
    import threading
    run = briefed(demand)
    demand.llm.started, demand.llm.release = threading.Event(), threading.Event()
    try:
        future, _ = demand.start('demand_design_generate', {'run_id': run['id'], 'brief_id': run['briefs'][0]['id']})
        assert demand.llm.started.wait(2)
        with pytest.raises(ValueError, match='执行中'):
            demand.start('demand_design_generate', {'run_id': run['id'], 'brief_id': run['briefs'][0]['id']})
        with pytest.raises(ValueError, match='执行中'):
            demand.demands.update(run['id'], INPUT)
    finally:
        demand.llm.release.set()
    assert future.result(timeout=10)['status'] == 'success'
    demand.llm.started = None
    demand.llm.bad = 'DEMAND_DESIGN'
    run = demand.demands.load(run['id'])
    run, status = start(demand, run, 'design_generate', brief_id=run['briefs'][0]['id'])
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    op['status'] = 'running'
    demand.demands.save(run)
    restored = DemandService(demand)
    restored.interrupt()
    assert restored.load(run['id'])['operations'][op['id']]['status'] == 'interrupted'
    demand.llm.bad = None
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success' and len(run['design_versions']) == 2
    demand.llm.bad = 'DEMAND_DESIGN'
    run, _ = start(demand, run, 'design_generate', brief_id=run['briefs'][0]['id'])
    failed = list(run['operations'])[-1]
    demand.demands.update(run['id'], {**INPUT, 'purpose': '新用途'})
    with pytest.raises(ValueError, match='已变化'):
        start(demand, run, 'retry', operation_id=failed)


def test_design_ui_generation_revision_sources_and_export(demand):
    run = briefed(demand)
    app = page(demand)
    assert not app.exception
    button(app, '生成设计方案').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ['整体概念', '视觉设计', '产品应用与交付']
    assert len(app.get('download_button')) == 5

    assert not next(e for e in app.expander if e.label == '已确认条件与设计假设').proto.expanded
    next(t for t in app.text_area if t.label == '设计方案修改意见').input('主色改为墨绿')
    button(app, '生成设计方案新版本').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert len(demand.demands.load(run['id'])['design_versions']) == 2
    assert any('#173F36' in m.value for m in app.markdown)
    calls = len(demand.llm.calls)
    app.run()
    assert len(demand.llm.calls) == calls
    demand.demands.update(run['id'], {**INPUT, 'scene': '婚礼'})
    app.run()
    assert not app.exception
    assert button(app, '生成设计方案').disabled
    assert button(app, '生成设计方案新版本').disabled
    assert len(app.get('download_button')) == 5


def test_design_facts_can_only_use_frozen_source_and_other_tasks_are_rejected(demand):
    run = briefed(demand)
    run['briefs'][0]['evidence'] = {'K1': {'title': '已保存原文', 'text': TEXT}}
    demand.demands.save(run)
    previous = demand.llm
    class CitingModel:
        settings, configured = previous.settings, True
        async def __call__(self, prompt, **kwargs):
            value = json.loads(await previous(prompt, **kwargs))
            value['facts'] = [{'text': '资料中提及丝绸展览', 'citations': [{'ref': 'K1', 'quote': '2026年10月09日举办丝绸展览。'}]}]
            return json.dumps(value)
    demand.llm = CitingModel()
    run, status = start(demand, run, 'design_generate', brief_id=run['briefs'][0]['id'])
    assert status['status'] == 'success'
    assert '2026年10月09日举办丝绸展览。' in export_markdown(run, run['design_versions'][0])
    other = demand.demands.create(INPUT)
    demand.demands.approve(other['id'])
    with pytest.raises(ValueError, match='请选择设计方案'):
        start(demand, other, 'design_revise', design_id=run['design_versions'][0]['id'], instruction='修改')
    with pytest.raises(ValueError, match='当前条件'):
        start(demand, other, 'design_generate', brief_id=run['briefs'][0]['id'])
    assert not demand.demands.load(other['id']).get('design_versions')


def test_ui_distinguishes_other_sources_without_model_calls(demand):
    run = designed(demand)
    first = run['design_versions'][0]
    run, _ = start(demand, run, 'revise', brief_id=run['briefs'][0]['id'], instruction='简化')
    run, _ = start(demand, run, 'design_generate', brief_id=run['briefs'][1]['id'])
    app = page(demand)
    history = next(s for s in app.selectbox if s.label == '设计方案历史版本')
    history.select(first['id']).run()
    assert not app.exception
    assert button(app, '生成设计方案新版本').disabled
    assert any('其他来源需求单' in x.value for x in app.info)
    calls = len(demand.llm.calls)
    next(s for s in app.selectbox if s.label == '需求单版本').select(run['briefs'][0]['id']).run()
    assert not app.exception
    assert not button(app, '生成设计方案新版本').disabled
    assert len(demand.llm.calls) == calls
