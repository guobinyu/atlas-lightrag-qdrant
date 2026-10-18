"""Explicit adoption, immutable sources and backwards-compatible intake."""
from copy import deepcopy
import json

import pytest

from test_demands import demand, work, INPUT, start
from test_demand_ui import page, button
from lab.demands import check_analysis, export_bundle


def analyzed(demand):
    run, status = start(demand, demand.demands.create(INPUT), 'analyze')
    assert status['status'] == 'success'
    analysis = run['clarifications'][-1]
    analysis.update(questions=[], suggestions={'audience': '参加聚会的成年校友'},
                    recommendations={'purpose': {'value': '重逢纪念赠礼', 'reason': '适合聚会的情感表达'},
                                     'theme': {'value': '相聚', 'reason': '与校友情谊相符'}})
    demand.demands.save(run)
    return run


def adoption(run, field='purpose', kind='recommendations'):
    return {'analysis_id': run['clarifications'][-1]['id'], 'field': field, 'kind': kind}


def test_analysis_custom_omits_search_window_but_trend_keeps_it(demand):
    run = analyzed(demand)
    assert 'days' not in demand.llm.inputs[-1]['inputs']
    assert run['inputs']['days'] == 7
    assert list(run['operations'].values())[-1]['input_snapshot']['inputs']['days'] == 7
    trend = demand.demands.create({**INPUT, 'path': 'trend'})
    trend, status = start(demand, trend, 'analyze')
    assert status['status'] == 'success'
    assert demand.llm.inputs[-1]['inputs']['days'] == 7


def test_adoption_is_explicit_bound_and_reaches_context_and_export(demand):
    run = analyzed(demand)
    source = deepcopy(run['clarifications'])
    run = demand.demands.confirm(run['id'], {**INPUT, 'purpose': '重逢纪念赠礼'}, 1, [adoption(run)])
    snapshot = run['summaries'][-1]['exploration']
    assert run['revision'] == 2
    assert run['clarifications'] == source
    assert snapshot['source_revision'] == 1
    assert snapshot['adoptions'][0]['value'] == run['inputs']['purpose']
    assert 'purpose' not in snapshot['recommendations']
    assert snapshot['recommendations']['theme']['value'] == '相聚'
    assert run['inputs']['theme'] == ''
    queries = []
    async def context(query):
        queries.append(query)
        return []
    demand.engine.campaign_context = context
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'success'
    assert '重逢纪念赠礼' in queries[0]
    assert '相聚' not in queries[0]
    assert run['candidate_rounds'][0]['context']['summary']['exploration'] == snapshot
    batch = run['candidate_rounds'][0]
    demand.demands.select_candidate(run['id'], batch['id'], 'C1')
    run, status = start(demand, run, 'brief')
    assert status['status'] == 'success'
    assert export_bundle(run, run['briefs'][0])['brief']['summary']['inputs']['purpose'] == '重逢纪念赠礼'
    assert 'DEMAND_SEARCH' in demand.llm.calls


def test_unadopted_suggestions_never_become_inputs(demand):
    run = analyzed(demand)
    result = demand.demands.confirm(run['id'], INPUT, 1)
    assert result['inputs'] == INPUT
    assert result['summaries'][-1]['exploration']['recommendations']['purpose']


@pytest.mark.parametrize('change', [{'capabilities': '只做纸盒'}, {'audience': '游客'}, {'purpose': '现场分享'}])
def test_reject_adoption_when_inputs_changed(demand, change):
    run = analyzed(demand)
    before = demand.demands._path(run['id']).read_bytes()
    with pytest.raises(ValueError, match='变化'):
        demand.demands.confirm(run['id'], {**INPUT, 'purpose': '重逢纪念赠礼', **change}, 1, [adoption(run)])
    assert demand.demands._path(run['id']).read_bytes() == before
    result = demand.demands.confirm(run['id'], {**INPUT, **change}, 1)
    assert result['summaries'][-1]['exploration']['source_analysis_id'] is None


def test_old_input_and_failed_analysis_are_read_and_retried_unchanged(demand):
    old = dict(INPUT)
    old.pop('purpose')
    run = demand.demands.create(INPUT)
    run['inputs'] = old
    run['summaries'][0]['inputs'] = deepcopy(old)
    demand.demands.save(run)
    demand.llm.bad = 'DEMAND_ANALYZE'
    run, status = start(demand, run, 'analyze')
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    op.pop('analysis_format_version')
    demand.demands.save(run)
    demand.llm.bad = None
    run, status = start(demand, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    assert run['clarifications'][-1]['analysis_format_version'] == 1
    assert 'recommendations' not in run['clarifications'][-1]
    assert run['inputs'] == old
    app = page(demand)
    assert not app.exception
    assert demand.demands.load(run['id'])['inputs'] == old


@pytest.mark.parametrize('rec', [None, [], {'brand': {'value': '虚构品牌', 'reason': '建议'}},
                               {'purpose': {'value': '赠礼'}}, {'purpose': {'value': '', 'reason': '建议'}}])
def test_invalid_recommendations_rejected(rec):
    with pytest.raises(ValueError):
        check_analysis({'known': [], 'unknown': [], 'questions': [], 'recommendations': rec}, 2)


def test_confirm_ui_adopts_only_on_final_confirmation(demand):
    run = analyzed(demand)
    app = page(demand)
    button(app, '继续完善需求').click().run()
    prefix = f"demand-confirm-{run['id']}-1-"
    app.button(key=prefix + 'recommendations-purpose').click().run()
    assert not app.exception
    assert app.text_input(key=prefix + 'purpose').value == '重逢纪念赠礼'
    assert demand.demands.load(run['id'])['inputs']['purpose'] == ''
    assert demand.demands.load(run['id'])['approved_revision'] is None
    button(app, '确认需求并继续').click().run()
    assert not app.exception
    result = demand.demands.load(run['id'])
    assert result['inputs']['purpose'] == '重逢纪念赠礼'
    assert result['summaries'][-1]['exploration']['adoptions'][0]['field'] == 'purpose'
    assert len(demand.llm.calls) == 1


def test_confirm_ui_manual_edit_invalidates_and_task_drafts_are_isolated(demand):
    run = analyzed(demand)
    other = demand.demands.create({**INPUT, 'request': '另一任务'})
    app = page(demand)
    app.selectbox(key='demand-history').select(run['id']).run()
    button(app, '继续完善需求').click().run()
    prefix = f"demand-confirm-{run['id']}-1-"
    app.button(key=prefix + 'recommendations-purpose').click().run()
    app.text_area(key=prefix + 'capabilities').input('只做标签').run()
    assert not app.exception
    assert app.button(key=prefix + 'recommendations-theme').disabled
    button(app, '确认需求并继续').click().run()
    result = demand.demands.load(run['id'])
    assert result['summaries'][-1]['exploration']['source_analysis_id'] is None
    assert 'adoptions' not in result['summaries'][-1]['exploration']
    assert demand.demands.load(other['id'])['inputs']['purpose'] == ''


def test_confirmation_dismiss_discards_draft_not_saved_answers(demand):
    run = analyzed(demand)
    app = page(demand)
    button(app, '继续完善需求').click().run()
    prefix = f"demand-confirm-{run['id']}-1-"
    app.button(key=prefix + 'recommendations-purpose').click().run()
    # Run the actual dismissal callback in the same session.
    from streamlit.testing.v1 import AppTest
    closing = AppTest.from_string('from lab.demand_ui import close_dialog; close_dialog()')
    for k, v in app.session_state._state.filtered_state.items():
        if k.startswith('demand-confirm-') or k == 'demand-dialog':
            closing.session_state[k] = v
    closing.run()
    assert not any(k.startswith('demand-confirm-') for k in closing.session_state._state.filtered_state)
    app = page(demand)
    button(app, '继续完善需求').click().run()
    assert app.text_input(key=prefix + 'purpose').value == ''
    assert demand.demands.load(run['id'])['approved_revision'] is None
