"""Four-step custom design flow: no hotspot search, fixed-path form, use date and the page end to end."""
import pytest

from lab.custom_ui import custom_state
from lab.demands import clean_inputs, current_brief, structured_lines
from test_demands import demand, work, INPUT, start
from test_demands_v4 import V4Model, culture
from test_ui_shell import shell, wait, button


def test_use_date_is_validated_shown_and_sent_to_the_model(demand):
    assert clean_inputs({**INPUT, 'use_date': '2026-12-20'})['use_date'] == '2026-12-20'
    assert 'use_date' not in clean_inputs(INPUT)  # Empty values keep old tasks equal.
    with pytest.raises(ValueError, match='使用日期'):
        clean_inputs({**INPUT, 'use_date': '2026-13-01'})
    inputs = clean_inputs({**INPUT, 'use_date': '2026-12-20', 'capacity': {'lead_days': 90}})
    line = next(l for l in structured_lines(inputs) if l.startswith('使用日期'))
    assert '距今 71 天' in line and '交期不足' in line
    run = demand.demands.create({**INPUT, 'use_date': '2026-12-20'})
    demand.demands.approve(run['id'])
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'success'
    assert demand.llm.inputs[-1]['summary']['inputs']['use_date'] == '2026-12-20'
    assert 'DEMAND_SEARCH' not in demand.llm.calls


def test_custom_state_follows_each_step(demand):
    demand.llm = V4Model(demand.llm)
    run = demand.demands.create(INPUT)
    assert custom_state(run)[:2] == ([False] * 4, 0)
    demand.demands.approve(run['id'])
    run, _ = start(demand, demand.demands.load(run['id']), 'candidates')
    assert custom_state(run)[1] == 1
    demand.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    run, _ = start(demand, demand.demands.load(run['id']), 'brief')
    done, current, brief, design = custom_state(run)
    assert current == 2 and brief and design is None
    assert custom_state(run, advanced=True)[1] == 3
    run, _ = start(demand, run, 'design_generate', brief_id=brief['id'], variants=3)
    done, current, _, design = custom_state(run)
    assert done[2] and current == 3 and design is None
    demand.demands.choose_design(run['id'], run['design_versions'][0]['id'])
    assert all(custom_state(demand.demands.load(run['id']))[0])


def test_new_task_view_locks_later_steps(demand):
    app = shell(demand, view='custom')
    assert not app.exception
    assert any('需先完成第 ① 步' in m.value for m in app.markdown)
    assert not any(b.label in ('生成产品方向', '生成设计说明') for b in app.button)


def test_page_walks_all_four_steps_without_search(demand):
    demand.llm = V4Model(demand.llm)
    culture(demand)
    app = shell(demand, view='custom')
    app.button(key='custom-pick-alumni').click().run()
    next(t for t in app.text_input if t.label == '行业（必填）').input('丝绸')
    next(t for t in app.text_area if t.label == '生产能力（必填）').input('真丝丝巾与礼盒')
    app.run()  # Fields commit on blur in the browser, which enables the chat input.
    app.chat_input(key='custom-chat-input').set_value('我要做校友纪念类产品').run()
    wait(demand)
    app.run()
    app.chat_input(key='custom-chat-input').set_value('预算中档').run()
    wait(demand)
    app.run()
    assert not app.exception
    # ① The summary form stays on this workbench: no path or hotspot range fields.
    assert not any(s.label in ('探索路径', '热点搜索时间范围') for s in app.selectbox)
    button(app, '确认需求并继续').click().run()
    assert not app.exception
    run = demand.demands.runs()[0]
    run = demand.demands.load(run['id'])
    assert run['inputs']['path'] == 'custom' and run['approved_revision'] == run['revision']
    # ② Directions come from the conversation, template and library; no hotspot search.
    app.button(key=f"custom-generate-{run['id']}").click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert 'DEMAND_SEARCH' not in demand.llm.calls and run['research'] == []
    round_id = run['candidate_rounds'][0]['id']
    app.button(key=f'pick-{round_id}-C1').click().run()
    assert not app.exception
    # ③ Pick elements, save without a model call, then generate the design brief.
    key = f"custom-pick-{run['id']}-{round_id}-C1-"
    app.pills(key=key + 'motifs').set_value(['M1']).run()
    calls = len(demand.llm.calls)
    button(app, '保存选择').click().run()
    run = demand.demands.load(run['id'])
    assert run['element_picks']['adopt'] == ['M1'] and len(demand.llm.calls) == calls
    demand.llm.refs['brief'] = 'M1'
    button(app, '生成设计说明').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    brief = current_brief(demand.demands.load(run['id']))
    assert brief['content']['story_mapping'][0]['element_ref'] == 'M1'
    app.button(key=f"custom-advance-{brief['id']}-button").click().run()
    assert not app.exception
    # ④ Three text versions by default, then confirm one.
    assert app.selectbox(key=f"custom-design-{run['id']}-{brief['id']}-variants").value == 3
    button(app, '生成三版设计方案').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert [v['variant'] for v in run['design_versions']] == [1, 2, 3]
    app.button(key='custom-choose-' + run['design_versions'][1]['id']).click().run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert run['design_choice'] == run['design_versions'][1]['id']
    assert all(custom_state(run)[0])
    assert 'DEMAND_SEARCH' not in demand.llm.calls


def test_edit_lists_impact_before_anything_changes(demand):
    demand.llm = V4Model(demand.llm)
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, _ = start(demand, demand.demands.load(run['id']), 'candidates')
    demand.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    run = demand.demands.load(run['id'])
    app = shell(demand, view='custom', demand=run['id'])
    app.button(key=f"custom-edit-0-{run['id']}-button").click().run()
    assert not app.exception
    assert any('1 轮候选' in w.value and '已选方向' in w.value for w in app.warning)
    assert demand.demands.load(run['id']) == run
    # ?step= opens a finished step without edit mode.
    app = shell(demand, view='custom', demand=run['id'], step='产品方向')
    assert any(b.label == '已选为方向' for b in app.button)
    assert not app.warning


def fill_merchant(app):
    app.text_input(key='custom-new-None-industry').input('丝绸')
    app.text_area(key='custom-new-None-capabilities').input('真丝丝巾与礼盒')
    app.run()


def test_template_starts_a_task_without_a_request(demand):
    app = shell(demand, view='custom')
    start_button = app.button(key='custom-start-template')
    assert start_button.disabled
    assert any('请先在上方选择一个场景模板' in c.value for c in app.caption)
    app.button(key='custom-pick-alumni').click().run()
    assert app.button(key='custom-start-template').disabled
    assert any('请先填写行业和生产能力' in c.value for c in app.caption)
    fill_merchant(app)
    assert not app.button(key='custom-start-template').disabled
    assert app.button(key='custom-start-template').label == '用「校友纪念品」模板开始'
    app.button(key='custom-start-template').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.runs()[0]
    assert run['inputs']['request'] == '定制校友纪念品'
    assert run['inputs']['template'] == 'alumni' and run['inputs']['audience'] == '返校校友'
    assert demand.llm.calls == ['DEMAND_ANALYZE']


def test_template_start_uses_the_optional_note(demand):
    app = shell(demand, view='custom')
    app.button(key='custom-pick-wedding').click().run()
    fill_merchant(app)
    app.text_area(key='custom-start-note').input('婚礼回礼丝巾，200 份').run()
    app.button(key='custom-start-template').click().run()
    wait(demand)
    run = demand.demands.runs()[0]
    assert run['inputs']['request'] == '婚礼回礼丝巾，200 份' and run['inputs']['template'] == 'wedding'


def test_switching_template_asks_first_and_lists_impact(demand):
    demand.llm = V4Model(demand.llm)
    run = demand.demands.create({**INPUT, 'template': 'alumni'})
    demand.demands.approve(run['id'])
    run, _ = start(demand, demand.demands.load(run['id']), 'candidates')
    run = demand.demands.load(run['id'])
    app = shell(demand, view='custom', demand=run['id'], step='需求对话')
    app.button(key=f"custom-switch-{run['id']}-wedding").click().run()
    assert not app.exception
    assert any('1 轮候选' in w.value for w in app.warning)
    assert demand.demands.load(run['id']) == run  # Nothing changes before confirming.
    app.button(key=f"custom-switch-cancel-{run['id']}").click().run()
    assert demand.demands.load(run['id']) == run
    app.button(key=f"custom-switch-{run['id']}-wedding").click().run()
    app.button(key=f"custom-switch-confirm-{run['id']}").click().run()
    assert not app.exception
    updated = demand.demands.load(run['id'])
    assert updated['inputs']['template'] == 'wedding' and updated['revision'] == run['revision'] + 1
    # Fields the user already had are kept; the requirement must be confirmed again.
    assert updated['inputs']['audience'] == INPUT['audience'] and updated['approved_revision'] is None
