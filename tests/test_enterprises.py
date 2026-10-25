"""Enterprise profiles, multi-topic selection, designer picks and the one-click pattern pipeline."""
import json

import pytest

from lab.demands import binding, round_markdown, export_markdown
from lab.enterprises import Enterprises
from test_demands import demand, work, INPUT, start
from test_demand_images import images
from test_demands_v4 import V4Model, ELEMENT, culture, briefed

PROFILE = {'name': '南浔某丝绸厂', 'industry': '丝绸', 'capabilities': '真丝丝巾与礼盒', 'constraints': '不做食品', 'brand': '',
           'capacity': {'categories': ['丝巾'], 'lead_days': 20}}


def test_profiles_validate_save_and_prefill(tmp_path):
    store = Enterprises(tmp_path / 'enterprises.json')
    saved = store.save(PROFILE)
    with pytest.raises(ValueError, match='同名'):
        store.save(PROFILE)
    with pytest.raises(ValueError, match='企业名称'):
        store.save({**PROFILE, 'name': ''})
    with pytest.raises(ValueError, match='正整数'):
        store.save({**PROFILE, 'name': '另一厂', 'capacity': {'lead_days': -1}})
    store.save({**PROFILE, 'capabilities': '真丝丝巾'}, saved['id'])
    fields = Enterprises.task_fields(store.get(saved['id']))
    assert fields['capabilities'] == '真丝丝巾' and 'name' not in fields and fields['capacity'] == {'categories': ['丝巾'], 'lead_days': 20}
    assert Enterprises.from_task('任务档案', {**INPUT, 'capacity': {'moq': 50}})['capacity'] == {'moq': 50}
    store.remove(saved['id'])
    assert store.list() == []


def test_multi_topic_selection_keeps_old_bindings_and_merges_context(demand):
    run = demand.demands.create({**INPUT, 'path': 'trend'})
    demand.demands.approve(run['id'])
    demand.demands.select_topic(run['id'], 'N-double11-2026')
    run = demand.demands.load(run['id'])
    # A single selection keeps the old string form, so earlier results stay current.
    assert binding(run)['topic_id'] == 'N-double11-2026' and run['topic_ids'] == ['N-double11-2026']
    manual = demand.demands.add_manual_topic(run['id'], '国潮丝巾走红')
    demand.demands.set_topics(run['id'], ['N-double11-2026', manual['id']])
    with pytest.raises(ValueError, match='1–3'):
        demand.demands.set_topics(run['id'], ['N-double11-2026', 'N-double12-2026', 'N-christmas-2026', manual['id']])
    with pytest.raises(ValueError, match='不同'):
        demand.demands.set_topics(run['id'], [manual['id'], manual['id']])
    run, status = start(demand, demand.demands.load(run['id']), 'candidates')
    assert status['status'] == 'success'
    context = run['candidate_rounds'][0]['context']
    assert [t['id'] for t in context['topics']] == ['N-double11-2026', manual['id']]
    assert set(context['topic_scores']) == {'N-double11-2026', manual['id']}
    assert {'N-double11-2026', manual['id']} <= set(context['evidence'])
    assert [t['id'] for t in demand.llm.inputs[-1]['topics']] == ['N-double11-2026', manual['id']]
    md = round_markdown(run, run['candidate_rounds'][0])
    assert '双十一' in md and '国潮丝巾走红' in md and '预测评分，非真实热度' in md
    demand.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    run, _ = start(demand, demand.demands.load(run['id']), 'brief')
    assert '国潮丝巾走红' in export_markdown(run, run['briefs'][0])


def test_legacy_task_without_topic_ids_loads_unchanged(demand):
    run = demand.demands.create({**INPUT, 'path': 'trend'})
    demand.demands.approve(run['id'])
    demand.demands.select_topic(run['id'], 'N-double11-2026')
    run, _ = start(demand, demand.demands.load(run['id']), 'candidates')
    raw = json.loads(demand.demands._path(run['id']).read_text(encoding='utf-8'))
    raw.pop('topic_ids')
    demand.demands._path(run['id']).write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    loaded = demand.demands.load(run['id'])
    assert loaded['topic_ids'] == ['N-double11-2026']
    assert loaded['candidate_rounds'][0]['binding'] == binding(loaded)


def test_picks_save_without_model_and_brief_requires_adopted_elements(demand):
    demand.llm = V4Model(demand.llm)
    culture(demand)
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, _ = start(demand, run, 'candidates')
    with pytest.raises(ValueError, match='产品候选'):
        demand.demands.save_picks(run['id'], {'adopt': ['M1']})
    demand.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    calls = len(demand.llm.calls)
    picks = demand.demands.save_picks(run['id'], {'adopt': ['M1'], 'style': '典雅', 'note': '偏好留白'})
    assert picks['adopt'] == ['M1'] and len(demand.llm.calls) == calls
    with pytest.raises(ValueError, match='本次资料'):
        demand.demands.save_picks(run['id'], {'adopt': ['M7']})
    run, status = start(demand, demand.demands.load(run['id']), 'brief', element_changes={'adopt': ['M1'], 'style': '典雅'})
    assert status['status'] == 'failed' and 'M1 需在 element_ref 中采用' in list(run['operations'].values())[-1]['responses'][-1]['error']
    demand.llm.refs['brief'] = 'M1'
    run, status = start(demand, run, 'retry', operation_id=list(run['operations'])[-1])
    assert status['status'] == 'success' and run['briefs'][0]['content']['story_mapping'][0]['element_ref'] == 'M1'


def test_pipeline_runs_brief_design_and_patterns_and_retries_only_failed_step(images):
    images.llm = V4Model(images.llm)
    run = images.demands.create(INPUT)
    images.demands.approve(run['id'])
    run, _ = start(images, run, 'candidates')
    images.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    original = images.images.render
    async def broken(*args, **kwargs):
        raise ValueError('图片服务暂时不可用')
    images.images.render = broken
    run, status = start(images, images.demands.load(run['id']), 'pipeline')
    assert status['status'] == 'failed'
    assert len(run['briefs']) == 1 and len(run['design_versions']) == 1 and not run.get('image_assets')
    calls = list(images.llm.calls)
    images.images.render = original
    run, status = start(images, run, 'retry', operation_id=list(run['operations'])[-1])
    assert status['status'] == 'success'
    assert images.llm.calls == calls  # Brief, design and pattern prompts were cached.
    assert len(run['briefs']) == 1 and len(run['design_versions']) == 1
    assert [a['mode'] for a in run['image_assets']] == ['pattern'] * 3
    assert run['design_versions'][0]['brief_id'] == run['briefs'][0]['id']


def test_pipeline_requires_selection_and_image_service(demand):
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    with pytest.raises(ValueError, match='产品候选'):
        start(demand, run, 'pipeline')
    run, _ = start(demand, run, 'candidates')
    demand.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    with pytest.raises(ValueError, match='图片服务'):
        start(demand, demand.demands.load(run['id']), 'pipeline')


def test_choose_design_marks_current_version_only(demand):
    demand.llm = V4Model(demand.llm)
    run = briefed(demand)
    run, _ = start(demand, run, 'design_generate', brief_id=run['briefs'][0]['id'], variants=3)
    second = run['design_versions'][1]['id']
    demand.demands.choose_design(run['id'], second)
    assert demand.demands.load(run['id'])['design_choice'] == second
    demand.demands.update(run['id'], {**INPUT, 'audience': '游客'})
    with pytest.raises(ValueError, match='当前条件'):
        demand.demands.choose_design(run['id'], second)
