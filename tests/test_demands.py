"""User-led workflow, isolation and checkpoint recovery with deterministic services."""
from copy import deepcopy
import json
import threading

import pytest

from test_campaigns import work, NOW, TEXT
from lab.demands import DemandService, stale, export_bundle, export_markdown, BRIEF_FIELDS

INPUT = {'request': '为校友聚会探索黄酒礼品', 'industry': '黄酒', 'capabilities': '黄酒酿造和礼盒包装',
         'brand': '', 'constraints': '不生产服饰', 'audience': '校友', 'scene': '聚会', 'theme': '', 'path': 'custom', 'days': 7}


class DemandModel:
    def __init__(self, previous):
        self.settings = previous.settings
        self.configured = True
        self.calls = []
        self.inputs = []
        self.bad = None
        self.started = None
        self.release = None

    async def __call__(self, prompt, **kwargs):
        stage = prompt.split('：')[0]
        data = json.loads(prompt.split('输入数据：\n')[1])
        self.calls.append(stage)
        self.inputs.append(data)
        if self.started:
            import asyncio
            self.started.set()
            await asyncio.to_thread(self.release.wait, 5)
        if self.bad == stage:
            return '{invalid'
        if stage == 'DEMAND_ANALYZE':
            value = {'known': ['用户可以生产黄酒'], 'unknown': ['价格未定'], 'questions': ['面向哪个价格区间？']}
        elif stage == 'DEMAND_SEARCH':
            value = {'queries': ['丝绸近期展览']}
        elif stage == 'DEMAND_EXTRACT':
            value = {'title': '丝绸展览', 'summary': '展览展示织造', 'published': None, 'event': {'date': '2026-10-09', 'quote': '2026年10月09日举办丝绸展览。'}}
        elif stage == 'DEMAND_GROUP':
            value = {'groups': [{'title': '丝绸展览', 'source_ids': ['S1'], 'fit': '与丝巾工艺相关'}]}
        elif stage == 'DEMAND_CANDIDATES':
            value = {'candidates': [{'name': f'建议礼品{i}', 'form': '礼盒', 'audience': '校友', 'scene': '聚会', 'theme': '重逢',
                'story': f'创意：以重逢的第{i}段记忆讲述校友情谊', 'visual_elements': '年份与校园剪影', 'reason': '作为纪念', 'capability_fit': '符合包装能力，需验证', 'design': '年份主题', 'assumptions': ['需求待验证'],
                'questions': ['价格待确定'], 'facts': []} for i in range(data['count'])]}
        elif stage == 'DEMAND_BRIEF':
            value = {'title': '校友聚会产品需求单', **{k: '建议：' + v for k, v in BRIEF_FIELDS.items()},
                     'assumptions': ['为设计建议'], 'questions': ['价格待确认'], 'facts': []}
            if '"followups"' in prompt:
                value.pop('questions')
                value['followups'] = [{'item': '品牌文件与参数文案', 'category': '资料补齐', 'owner': '商家',
                                       'timing': '包装定稿前', 'approach': '概念稿先保留位置，收到确认文案后排版'}]
        else:
            raise AssertionError(stage)
        return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def demand(work, monkeypatch):
    import lab.demands as module
    monkeypatch.setattr(module, 'utc_now', lambda: NOW)
    monkeypatch.setattr(module, 'search_web', lambda *a: [{'url': 'https://example.com/story', 'title': '展览'}])
    monkeypatch.setattr(module, 'fetch_source', lambda url, title: {'url': url, 'title': title, 'text': TEXT, 'collected_at': NOW})
    work.llm = DemandModel(work.llm)
    work.demands = DemandService(work)
    return work


def start(work, run, kind, **inputs):
    future, job = work.start('demand_' + kind, {'run_id': run['id'], **inputs})
    status = future.result(timeout=15)
    return work.demands.load(run['id']), status


def candidates(work, inputs=None):
    run = work.demands.create(inputs or INPUT)
    work.demands.approve(run['id'])
    run, status = start(work, run, 'candidates')
    assert status['status'] == 'success'
    return run


def selected(work):
    run = candidates(work)
    batch = run['candidate_rounds'][0]
    work.demands.select_candidate(run['id'], batch['id'], 'C1')
    return work.demands.load(run['id'])


def test_custom_never_searches_and_preserves_user_facts(demand, monkeypatch):
    def forbidden(*args):
        raise AssertionError('定制流程不应搜索网页')
    monkeypatch.setattr('lab.demands.search_web', forbidden)
    run = candidates(demand)
    assert run['inputs'] == INPUT
    assert run['inputs']['brand'] == ''
    assert run['candidate_rounds'][0]['context']['knowledge_missing']
    assert len(run['candidate_rounds'][0]['candidates']) == 3
    assert demand.llm.calls == ['DEMAND_CANDIDATES']


def test_clarify_answers_and_explicit_continue(demand):
    run = demand.demands.create(INPUT)
    with pytest.raises(ValueError, match='继续'):
        start(demand, run, 'candidates')
    run, status = start(demand, run, 'analyze')
    assert status['status'] == 'success'
    assert len(run['clarifications']) == 1
    run = demand.demands.update(run['id'], INPUT, {'价格？': '暂不确定，请给建议'})
    run, _ = start(demand, run, 'analyze')
    assert len(run['clarifications']) == 2
    assert demand.llm.inputs[-1]['answers']['价格？'] == '暂不确定，请给建议'
    assert run['inputs'] == INPUT
    assert not run['candidate_rounds']


def test_custom_requires_one_context_field_and_capabilities(demand):
    with pytest.raises(ValueError, match='生产能力'):
        demand.demands.create({**INPUT, 'capabilities': ''})
    run = demand.demands.create({**INPUT, 'audience': '', 'scene': '', 'theme': ''})
    with pytest.raises(ValueError, match='至少'):
        demand.demands.approve(run['id'])


def test_trend_waits_for_topic_then_candidates(demand):
    run = demand.demands.create({**INPUT, 'path': 'trend', 'industry': '丝绸', 'capabilities': '丝巾和家居服', 'constraints': ''})
    demand.demands.approve(run['id'])
    with pytest.raises(ValueError, match='话题'):
        start(demand, run, 'candidates')
    run, status = start(demand, run, 'search')
    assert status['status'] == 'success'
    assert not run['candidate_rounds'] and run['topic_id'] is None
    topic = run['research'][0]['topics'][0]
    assert topic['date_status'] == 'verified'
    assert topic['heat'] == '热度未验证'
    demand.demands.select_topic(run['id'], topic['id'])
    run, status = start(demand, run, 'candidates', count=5)
    assert status['status'] == 'success'
    assert len(run['candidate_rounds'][0]['candidates']) == 5
    assert 'S1' in run['candidate_rounds'][0]['context']['evidence']
    assert all('INFER_BRAND' != c for c in demand.llm.calls)


def test_no_sources_stops_and_retry_recovers(demand, monkeypatch):
    original = __import__('lab.demands', fromlist=['search_web']).search_web
    monkeypatch.setattr('lab.demands.search_web', lambda *a: [])
    run = demand.demands.create({**INPUT, 'path': 'trend'})
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'search')
    assert status['status'] == 'failed'
    assert not run['research'] and not run['candidate_rounds']
    monkeypatch.setattr('lab.demands.search_web', original)
    # An explicit new search refreshes previously successful empty search responses.
    run, status = start(demand, run, 'search')
    assert status['status'] == 'success'


def test_invalid_response_retry_reuses_completed_knowledge(demand):
    calls = []
    async def context(q):
        calls.append(q)
        return []
    demand.engine.campaign_context = context
    demand.llm.bad = 'DEMAND_CANDIDATES'
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'failed'
    assert not run['candidate_rounds']
    op = list(run['operations'].values())[-1]
    assert op['responses'][0]['error']
    demand.llm.bad = None
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success' and len(calls) == 1
    assert len(run['candidate_rounds']) == 1


def test_retrieval_failure_is_not_empty_knowledge(demand):
    async def broken(q):
        raise ValueError('品牌知识检索失败 TEST_CAMPAIGN_SECRET')
    demand.engine.campaign_context = broken
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'failed'
    assert 'TEST_CAMPAIGN_SECRET' not in status['message']
    assert not demand.llm.calls


def test_brief_revision_exports_and_old_versions(demand):
    run = selected(demand)
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    original = deepcopy(run['briefs'][0])
    run, status = start(demand, run, 'revise', brief_id=original['id'], instruction='增加简约设计建议')
    assert status['status'] == 'success'
    assert len(run['briefs']) == 2 and run['briefs'][0] == original
    md = export_markdown(run, original)
    assert '品牌名称：未提供' in md and INPUT['capabilities'] in md
    bundle = export_bundle(run, original)
    assert bundle['brief']['selection'] == run['selection']
    assert bundle['brief']['summary']['inputs'] == INPUT
    assert bundle['task_id'] == run['id']


def test_upstream_changes_mark_history_and_invalidate_retry(demand):
    run = selected(demand)
    run, _ = start(demand, run, 'brief')
    run = demand.demands.update(run['id'], {**INPUT, 'audience': '新婚家庭'})
    assert run['selection'] is None
    assert stale(run, run['candidate_rounds'][0]) and stale(run, run['briefs'][0])
    with pytest.raises(ValueError, match='当前条件'):
        demand.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'success'
    assert run['candidate_rounds'][-1]['context']['summary']['inputs']['audience'] == '新婚家庭'


def test_candidate_revision_passes_previous_round(demand):
    run = candidates(demand)
    run, status = start(demand, run, 'candidates', instruction='减少包装材料')
    assert status['status'] == 'success'
    assert len(run['candidate_rounds']) == 2
    assert demand.llm.inputs[-1]['previous_candidates'] == run['candidate_rounds'][0]['candidates']


def test_copy_only_merchant_and_workspace_validation(demand):
    run = selected(demand)
    copied = demand.demands.merchant(run['id'])
    assert set(copied) == {'industry', 'capabilities', 'brand', 'constraints'}
    second = demand.demands.create({**INPUT, **copied})
    assert not second['candidate_rounds'] and not second['selection'] and not second['research']
    run['workspace'] = 'another'
    demand.demands.save(run)
    with pytest.raises(ValueError, match='工作区'):
        demand.demands.load(run['id'])
    with pytest.raises(ValueError, match='标识'):
        demand.demands.load('../escape')


def test_lock_blocks_double_submission_and_mutation(demand):
    run = demand.demands.create(INPUT)
    demand.llm.started, demand.llm.release = threading.Event(), threading.Event()
    future, _ = demand.start('demand_analyze', {'run_id': run['id']})
    try:
        assert demand.llm.started.wait(3)
        with pytest.raises(ValueError, match='执行中'):
            demand.start('demand_analyze', {'run_id': run['id']})
        with pytest.raises(ValueError, match='执行中'):
            demand.demands.update(run['id'], INPUT)
    finally:
        demand.llm.release.set()
        future.result(timeout=10)
    assert not demand.engine.gate.locked()


def test_interrupt_preserves_checkpoint_and_requires_explicit_retry(demand):
    run = demand.demands.create(INPUT)
    run, op = demand.demands.prepare('demand_analyze', {'run_id': run['id']})
    demand.demands.interrupt()
    run = demand.demands.load(run['id'])
    assert run['operations'][op['id']]['status'] == 'interrupted'
    assert not demand.llm.calls
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'


def test_fact_validation_rejects_unknown_source_and_unlocatable_quote():
    from lab.demands import check_facts
    value = {'facts': [{'text': '记载', 'citations': [{'ref': 'S1', 'quote': '不存在'}]}]}
    with pytest.raises(ValueError, match='引文'):
        check_facts(value, {'S1': {'text': '原文'}})
    with pytest.raises(ValueError, match='引用'):
        check_facts(value, {})
    value['facts'][0]['citations'][0]['quote'] = '原文'
    check_facts(value, {'S1': {'text': '原文'}})


def test_out_of_range_topic_remains_cultural_reference_in_export(demand, monkeypatch):
    monkeypatch.setattr('lab.demands.utc_now', lambda: '2027-01-01T12:00:00+08:00')
    run = demand.demands.create({**INPUT, 'path': 'trend'})
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'search')
    assert status['status'] == 'success'
    assert run['research'][-1]['topics'][0]['date_status'] == 'excluded'
    demand.demands.select_topic(run['id'], 'T1')
    run, _ = start(demand, run, 'candidates')
    demand.demands.select_candidate(run['id'], run['candidate_rounds'][-1]['id'], 'C1')
    run, _ = start(demand, run, 'brief')
    brief = run['briefs'][-1]
    assert brief['topic']['date_status'] == 'excluded'
    assert '非已核实近期话题，仅作文化参考' in export_markdown(run, brief)


def test_dialog_draft_resume_atomic_submit_and_no_duplicates(demand):
    run, _ = start(demand, demand.demands.create(INPUT), 'analyze')
    service = demand.demands
    analysis = run['clarifications'][-1]
    analysis['questions'] = [{'text': '产品方向？', 'options': ['丝巾', '家居服', '组合系列']}, '使用场景？']
    service.save(run)
    session = service.clarification_session(run['id'], analysis['id'])
    with pytest.raises(ValueError, match='填写回答'):
        service.save_answer(run['id'], session['id'], 0)
    service.save_answer(run['id'], session['id'], 0, '组合系列', '数码印花', cursor=1)
    restored = DemandService(demand).clarification_session(run['id'], analysis['id'])
    assert restored['cursor'] == 1
    assert restored['answers']['Q1']['text'] == '数码印花'
    assert service.load(run['id'])['revision'] == 1
    with pytest.raises(ValueError, match='完成本轮'):
        start(demand, run, 'analyze', session_id=session['id'])
    service.save_answer(run['id'], session['id'], 1, unknown=True)
    demand.llm.bad = 'DEMAND_ANALYZE'
    run, status = start(demand, run, 'analyze', session_id=session['id'])
    assert status['status'] == 'failed'
    assert run['revision'] == 2
    assert run['inputs'] == INPUT
    assert run['summaries'][-1]['answers']['使用场景？'] == '暂不确定，请给建议'
    with pytest.raises(ValueError):
        start(demand, run, 'analyze', session_id=session['id'])
    demand.llm.bad = None
    op = list(run['operations'].values())[-1]
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    assert run['revision'] == 2
    assert len(demand.llm.calls) == 3


def test_dialog_stale_and_cross_task_rejected(demand):
    service = demand.demands
    run, _ = start(demand, service.create(INPUT), 'analyze')
    session = service.clarification_session(run['id'], run['clarifications'][-1]['id'])
    other = service.create(INPUT)
    with pytest.raises(ValueError):
        service.save_answer(other['id'], session['id'], 0, text='回答')
    service.update(run['id'], {**INPUT, 'audience': '新用户'})
    with pytest.raises(ValueError, match='需求已变化'):
        service.save_answer(run['id'], session['id'], 0, text='回答')
    with pytest.raises(ValueError, match='需求已变化'):
        service.confirm(run['id'], INPUT, 1)


def test_confirmation_keeps_saved_drafts_and_validates_before_writing(demand):
    service = demand.demands
    run, _ = start(demand, service.create(INPUT), 'analyze')
    session = service.clarification_session(run['id'], run['clarifications'][-1]['id'])
    service.save_answer(run['id'], session['id'], 0, unknown=True)
    with pytest.raises(ValueError, match='至少明确'):
        service.confirm(run['id'], {**INPUT, 'audience': '', 'scene': ''}, 1)
    assert service.load(run['id'])['revision'] == 1
    result = service.confirm(run['id'], INPUT, 1)
    assert result['revision'] == result['approved_revision'] == 2
    assert result['summaries'][-1]['answers']
    assert result['inputs']['brand'] == ''
    assert demand.llm.calls == ['DEMAND_ANALYZE']


@pytest.mark.parametrize('value', [[{'text': '问题', 'options': ['重复', '重复']}], [{'text': '问题', 'options': '错误'}], ['问题'] * 4])
def test_structured_question_validation(value):
    from lab.demands import questions
    with pytest.raises(ValueError):
        questions(value)


def test_clarification_five_round_limit_and_final_retry(demand):
    from lab.demands import clarification_rounds
    service = demand.demands
    run = service.create(INPUT)
    # A model which keeps asking new questions must still stop after round five.
    class PersistentModel(DemandModel):
        async def __call__(self, prompt, **kwargs):
            if self.bad:
                return '{invalid'
            self.calls.append(prompt)
            return json.dumps({'known': ['已知能力'], 'unknown': ['预算待探索'],
                               'questions': [{'text': f'问题{len(self.calls)}', 'options': ['选项']}]})
    demand.llm = PersistentModel(demand.llm)
    run, _ = start(demand, run, 'analyze')
    for number in range(1, 6):
        session = service.clarification_session(run['id'], run['clarifications'][-1]['id'])
        assert session['round'] == number
        service.save_answer(run['id'], session['id'], 0, unknown=True)
        if number == 5:
            demand.llm.bad = True
        run, status = start(demand, run, 'analyze', session_id=session['id'])
    assert status['status'] == 'failed'
    demand.llm.bad = False
    run, status = start(demand, run, 'retry', operation_id=list(run['operations'])[-1])
    assert status['status'] == 'success'
    assert len(clarification_rounds(run)) == 5
    assert run['clarifications'][-1]['questions'] == []
    assert '问题6' in run['clarifications'][-1]['unknown']
    with pytest.raises(ValueError, match='最多 5 轮'):
        start(demand, run, 'analyze')
    service.update(run['id'], {**INPUT, 'audience': '新用户'})
    with pytest.raises(ValueError, match='最多 5 轮'):
        start(demand, run, 'analyze')
    current = service.load(run['id'])
    service.confirm(run['id'], current['inputs'], current['revision'])
    assert service.load(run['id'])['approved_revision'] == current['revision']
