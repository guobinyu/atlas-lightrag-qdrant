"""Festival calendar, prediction score components and ranking without model calls."""
from datetime import date

import pytest

from lab import demand_scoring as s
from test_demands import demand, work, INPUT, start


def run_with(capacity=None, scoring=None):
    return {'inputs': {'capacity': capacity or {}}, 'scoring': scoring or {}}


def test_builtin_calendar_dates_and_rules():
    nodes = {n['key']: n['date'] for n in s.builtins()}
    assert nodes['mothers-2027'] == '2027-05-09' and nodes['fathers-2027'] == '2027-06-20'
    assert nodes['chunjie-2027'] == '2027-02-06' and nodes['zhongqiu-2027'] == '2027-09-15'
    assert nodes['double11-2026'] == '2026-11-11' and nodes['yuandan-2027'] == '2027-01-01'
    assert 'national-2026' not in nodes  # Before the first built-in day.
    assert [n['date'] for n in s.builtins()] == sorted(n['date'] for n in s.builtins())


def test_calendar_edits_are_workspace_local(tmp_path):
    calendar = s.Calendar(tmp_path / 'calendar.json')
    today = date(2026, 10, 10)
    assert {n['key'] for n in calendar.upcoming(today)} >= {'double11-2026', 'chunjie-2027'}
    assert all(n['date'] <= '2027-02-07' for n in calendar.upcoming(today))
    node = calendar.add('校庆日', '2026-11-20', '消费节点', '母校 120 周年')
    assert calendar.get(node['key'])['origin'] == 'custom'
    calendar.remove('double11-2026')
    calendar.remove(node['key'])
    assert calendar.get('double11-2026') is None and calendar.get(node['key']) is None
    assert s.Calendar(tmp_path / 'calendar.json').get('double11-2026') is None
    with pytest.raises(ValueError, match='日期'):
        calendar.add('无效', '2026-13-01', '节日')
    with pytest.raises(ValueError, match='不存在'):
        calendar.remove('missing')


@pytest.mark.parametrize('days,lead,expected,flag', [
    (32, None, 1.0, f'未设置交期，按 {s.DEFAULT_LEAD} 天估算'), (32, 40, 0.0, '交期不足'), (-1, 30, 0.0, '节点已过'),
    (75, 30, 1.0, None), (120, 30, 0.5, None), (300, 30, 0.0, None)])
def test_proximity_window(days, lead, expected, flag):
    today = date(2026, 10, 10)
    topic = {'id': 'N-x', 'kind': 'calendar', 'date': date.fromordinal(today.toordinal() + days).isoformat()}
    parts, flags = s.components(topic, lead, None, today)
    assert parts == {'proximity': expected}
    assert (flag in flags) if flag else not [f for f in flags if '交期' in f or '已过' in f]


def test_score_renormalizes_missing_components_and_uses_weights():
    today = date(2026, 10, 10)
    search = {'id': 'T1', 'kind': 'search', 'date_status': 'unverified', 'fit_score': 5}
    result = s.score(search, run_with(), today)
    assert result['components'] == {'timeliness': 0.5, 'fit': 1.0}
    assert result['score'] == round((0.25 * 0.5 + 0.25 * 1.0) / 0.5, 3)
    assert result['note'] == s.NOTE
    heated = s.score(search, run_with(scoring={'weights': {**s.WEIGHTS, 'heat': 1.0}, 'heat': {'T1': 1}}), today)
    assert heated['components']['heat'] == 0.2 and heated['score'] < result['score']
    assert s.score({'id': 'U1', 'kind': 'manual', 'date': ''}, run_with(), today)['score'] is None
    legacy = {'id': 'T2', 'date_status': 'verified'}  # Saved before topics had kind or fit_score.
    assert s.score(legacy, run_with(), today)['components'] == {'timeliness': 1.0}


@pytest.mark.parametrize('weights,heat', [({'timeliness': 1}, {}), ({**s.WEIGHTS, 'fit': 2}, {}),
                                          ({k: 0 for k in s.WEIGHTS}, {}), (s.WEIGHTS, {'T1': 6}), (s.WEIGHTS, {'T1': True})])
def test_invalid_scoring_rejected(weights, heat):
    with pytest.raises(ValueError):
        s.check(weights, heat)


def test_ranking_and_weights_never_call_model_or_change_revision(demand):
    run = demand.demands.create({**INPUT, 'capacity': {'lead_days': 20}})
    demand.demands.approve(run['id'])
    run = demand.demands.load(run['id'])
    topics = demand.demands.topics(run)
    assert topics and all(t['kind'] == 'calendar' for t in topics)
    first = topics[0]
    assert first['score']['score'] == max(t['score']['score'] for t in topics)
    calls, revision = len(demand.llm.calls), run['revision']
    christmas = next(t for t in topics if t['title'] == '圣诞节')
    demand.demands.set_scoring(run['id'], {'timeliness': 0, 'proximity': 0.1, 'fit': 0, 'heat': 1}, {christmas['id']: 5})
    run = demand.demands.load(run['id'])
    assert demand.demands.topics(run)[0]['id'] == christmas['id']
    assert next(t for t in demand.demands.topics(run) if t['title'] == '双十一')['score']['components']['heat'] == 0
    assert len(demand.llm.calls) == calls and run['revision'] == revision and run['approved_revision'] == revision
