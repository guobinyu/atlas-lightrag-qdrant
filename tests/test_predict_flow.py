"""Five-step hotspot prediction flow: step states, topic-preserving edits and the page driven end to end."""
from lab.demands import binding, current_brief, stale
from lab.predict_ui import flow_state
from test_demands import demand, work, INPUT, start
from test_demand_images import images
from test_demands_v4 import V4Model
from test_ui_shell import shell, wait, button

TREND = {**INPUT, 'path': 'trend'}


def topic_run(work):
    run = work.demands.create(TREND)
    work.demands.approve(run['id'])
    work.demands.set_topics(run['id'], ['N-double11-2026'])
    return work.demands.load(run['id'])


def directed(work):
    run, status = start(work, topic_run(work), 'candidates')
    assert status['status'] == 'success'
    work.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    return work.demands.load(run['id'])


def test_flow_state_follows_each_step(demand):
    demand.llm = V4Model(demand.llm)
    run = demand.demands.create(TREND)
    assert flow_state(run)[:2] == ([False] * 5, 0)
    demand.demands.approve(run['id'])
    assert flow_state(demand.demands.load(run['id']))[1] == 1
    assert flow_state(topic_run(demand))[1] == 2  # Another task; confirmed topics finish step ②.
    run = directed(demand)
    assert flow_state(run)[1] == 3
    run, _ = start(demand, run, 'brief')
    done, current, brief, design = flow_state(run)
    assert current == 3 and brief['id'] == run['briefs'][0]['id'] and design is None
    assert flow_state(run, advanced=True)[1] == 4
    run, _ = start(demand, run, 'design_generate', brief_id=brief['id'], variants=3)
    # Three unconfirmed variants: the package is done but no design is current yet.
    done, current, _, design = flow_state(run)
    assert done[3] and current == 4 and design is None
    demand.demands.choose_design(run['id'], run['design_versions'][1]['id'])
    done, current, _, design = flow_state(demand.demands.load(run['id']))
    assert all(done) and current is None and design['id'] == run['design_versions'][1]['id']


def test_capacity_edit_keeps_topics_but_requirement_edit_clears_them(demand):
    demand.llm = V4Model(demand.llm)
    run = demand.demands.create(TREND)
    demand.demands.approve(run['id'])
    run, status = start(demand, demand.demands.load(run['id']), 'search')
    assert status['status'] == 'success'
    demand.demands.set_topics(run['id'], ['T1', 'N-double11-2026'])
    run = directed_from(demand, run['id'])
    old_round = run['candidate_rounds'][0]
    run = demand.demands.confirm(run['id'], {**run['inputs'], 'capacity': {'lead_days': 25}}, run['revision'])
    assert run['topic_ids'] == ['T1', 'N-double11-2026'] and run['research_id'] and run['selection'] is None
    assert stale(run, old_round)
    assert any(t['id'] == 'T1' for t in demand.demands.topics(run))
    run = demand.demands.confirm(run['id'], {**run['inputs'], 'request': '改为预测春节礼品'}, run['revision'])
    assert run['topic_ids'] == [] and run['research_id'] is None


def directed_from(work, identifier):
    run, status = start(work, work.demands.load(identifier), 'candidates')
    assert status['status'] == 'success'
    work.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    return work.demands.load(run['id'])


def test_research_saved_before_search_keys_follows_the_old_rule(demand):
    run = demand.demands.create(TREND)
    demand.demands.approve(run['id'])
    run, _ = start(demand, demand.demands.load(run['id']), 'search')
    run['research'][0].pop('search_key')
    demand.demands.save(run)
    demand.demands.set_topics(run['id'], ['T1'])
    run = demand.demands.load(run['id'])
    run = demand.demands.confirm(run['id'], {**run['inputs'], 'capacity': {'lead_days': 25}}, run['revision'])
    assert run['topic_ids'] == [] and run['research_id'] is None


def test_page_walks_all_five_steps(demand):
    demand.llm = V4Model(demand.llm)
    app = shell(demand, view='predict')
    button(app, '新建研判任务').click().run()
    app.text_area(key='predict-new-None-request').input('预测节日丝巾')
    app.text_input(key='predict-new-None-industry').input('丝绸')
    app.text_area(key='predict-new-None-capabilities').input('真丝丝巾与礼盒')
    app.button(key='predict-new-None-create').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.runs()[0]
    # ① The model's question is answered in the page; later steps are locked.
    assert any('需先完成第 ① 步' in m.value for m in app.markdown)
    assert not any(b.label == '确认选题并生成候选' for b in app.button)
    next(t for t in app.text_area if t.label == '补充说明／其他回答').input('中档')
    button(app, '提交本轮回答').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert run['approved_revision'] == run['revision']
    # ② Confirming topics starts the candidates directly.
    app.checkbox(key=f"predict-topic-{run['id']}-N-double11-2026").check()
    button(app, '确认选题并生成候选').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert run['topic_ids'] == ['N-double11-2026'] and len(run['candidate_rounds']) == 1
    # ③ Picking a direction without the auto option only opens step ④.
    round_id = run['candidate_rounds'][0]['id']
    assert app.checkbox(key=f"predict-auto-{run['id']}").value is False
    app.button(key=f'pick-{round_id}-C1').click().run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert run['selection'] == {'round_id': round_id, 'candidate_id': 'C1'} and not run['briefs']
    # ④ Generate the package, then move on explicitly.
    app.button(key=f"predict-brief-generate-{run['id']}").click().run()
    wait(demand)
    app.run()
    assert not app.exception
    brief = current_brief(demand.demands.load(run['id']))
    app.button(key=f"predict-advance-{brief['id']}-button").click().run()
    assert not app.exception
    # ⑤ Without an image service the primary action generates the text design only.
    app.button(key=f"predict-design-{run['id']}-{brief['id']}-generate").click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    done, current, _, design = flow_state(run, advanced=True)
    assert all(done) and design and not run.get('image_assets')
    assert not app.error, [e.value for e in app.error]
    assert {'候选轮次', '方案包', '设计方案'} <= {t.label for t in app.tabs}  # Version record.


def test_edit_lists_impact_before_anything_changes(demand):
    demand.llm = V4Model(demand.llm)
    run = directed(demand)
    app = shell(demand, view='predict', demand=run['id'])
    app.button(key=f"predict-edit-1-{run['id']}-button").click().run()
    assert not app.exception
    assert any('1 轮候选' in w.value and '已选方向' in w.value for w in app.warning)
    assert demand.demands.load(run['id']) == run
    # ?step= opens a finished step without entering edit mode.
    app = shell(demand, view='predict', demand=run['id'], step='选题')
    assert any(b.label == '确认选题并生成候选' for b in app.button)
    assert not app.warning


def test_auto_option_runs_package_design_and_patterns(images):
    images.llm = V4Model(images.llm)
    run, _ = start(images, topic_run(images), 'candidates')
    round_id = run['candidate_rounds'][0]['id']
    app = shell(images, view='predict', demand=run['id'])
    app.checkbox(key=f"predict-auto-{run['id']}").check()
    app.button(key=f'pick-{round_id}-C1').click().run()
    wait(images)
    app.run()
    assert not app.exception
    run = images.demands.load(run['id'])
    assert len(run['briefs']) == 1 and len(run['design_versions']) == 1
    assert [a['mode'] for a in run['image_assets']] == ['application'] * 3
    assert all(flow_state(run)[0])
    assert run['briefs'][0]['binding'] == binding(run)
