"""Top navigation, legacy links, home page and the two workbenches with deterministic services."""
import time

from streamlit.testing.v1 import AppTest

from test_demands import demand, work, INPUT, start
from test_demands_v4 import V4Model, ELEMENT, culture

SHELL = '''
import streamlit as st
from lab.workbench_ui import navigation, render
engine = st.session_state.test_engine
render(engine, navigation(engine))
'''


def shell(work, **params):
    app = AppTest.from_string(SHELL)
    app.session_state['test_engine'] = work.engine
    for key, value in params.items():
        app.query_params[key] = value
    return app.run(timeout=20)


def button(app, label):
    return next(b for b in app.button if b.label == label)


def wait(work):
    deadline = time.monotonic() + 5
    while work.active is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert work.active is None


def test_home_is_default_with_entries_recent_tasks_and_calendar(demand):
    run = demand.demands.create({**INPUT, 'path': 'trend'})
    app = shell(demand)
    assert not app.exception
    assert app.segmented_control(key='top-nav').value == 'home'
    for label in ('进入爆款预测', '进入定制设计', '进入知识库', '进入纹样设计', '进入营销生成'):
        assert any(b.label == label for b in app.button)
    assert any('双十一' in m.value for m in app.markdown)
    app.button(key='home-open-' + run['id']).click().run()
    assert app.query_params['view'] == ['predict'] and app.query_params['demand'] == [run['id']]
    assert not app.exception


def test_top_nav_and_system_menu_switch_pages(demand):
    app = shell(demand)
    app.segmented_control(key='top-nav').set_value('custom').run()
    assert not app.exception
    assert app.query_params['view'] == ['custom']
    assert any('特色个性化产品定制工作台' in m.value for m in app.markdown)
    app.button(key='system-settings').click().run()
    assert not app.exception
    assert app.query_params['view'] == ['settings']
    assert [t.label for t in app.tabs][:5] == ['文字模型', '图片模型', '搜索服务', '企业档案', '节日日历']
    assert app.segmented_control(key='top-nav').value is None


def test_legacy_demand_links_open_the_tasks_workbench(demand):
    trend = demand.demands.create({**INPUT, 'path': 'trend'})
    custom = demand.demands.create(INPUT)
    app = shell(demand, view='demand', demand=trend['id'])
    assert not app.exception and app.query_params['view'] == ['predict']
    assert app.selectbox(key='trend-task').value == trend['id']
    app = shell(demand, view='demand', demand=custom['id'])
    assert not app.exception and app.query_params['view'] == ['custom']
    assert app.selectbox(key='custom-task').value == custom['id']
    assert not shell(demand, view='unknown').exception


def test_predict_board_multi_select_cards_and_export(demand):
    demand.llm = V4Model(demand.llm)
    run = demand.demands.create({**INPUT, 'path': 'trend', 'capacity': {'categories': ['礼盒包装'], 'lead_days': 20}})
    demand.demands.approve(run['id'])
    app = shell(demand, view='predict')
    assert not app.exception
    assert len(app.get('plotly_chart')) == 1
    assert button(app, '生成备选方案').disabled  # No topic yet.
    app.checkbox(key=f"predict-topic-{run['id']}-N-double11-2026").check()
    app.checkbox(key=f"predict-topic-{run['id']}-N-christmas-2026").check()
    assert any('尚未确认选题' in i.value for i in app.info)
    button(app, '确认选题').click().run()
    assert not app.exception
    assert demand.demands.load(run['id'])['topic_ids'] == ['N-double11-2026', 'N-christmas-2026']
    assert any('已确认 2 个选题' in t.value for t in app.toast)
    assert any('当前选题：双十一、圣诞节' in x.value for x in app.success)
    button(app, '确认选题').click().run()
    assert any('选题没有变化' in t.value for t in app.toast)
    button(app, '生成备选方案').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    round_id = demand.demands.load(run['id'])['candidate_rounds'][0]['id']
    assert any(':material/redeem:' in m.value for m in app.markdown)
    assert any('产能通过' in m.value for m in app.markdown)
    app.button(key=f'pick-{round_id}-C1').click().run()
    assert demand.demands.load(run['id'])['selection'] == {'round_id': round_id, 'candidate_id': 'C1'}
    exported = next(d for d in app.get('download_button') if d.proto.label == '导出方案')
    assert not exported.proto.disabled
    # Pattern dispatch needs an image service.
    assert button(app, '下发纹样任务').disabled
    # The next step is reachable from the research tab and opens the package tab.
    assert any('已选方向' in c.value for c in app.caption)
    button(app, '生成方案包').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert app.query_params['step'] == ['方案包']
    assert len(demand.demands.load(run['id'])['briefs']) == 1


def test_predict_new_task_from_enterprise_profile(demand):
    demand.enterprises.save({'name': '南浔某厂', 'industry': '丝绸', 'capabilities': '真丝丝巾', 'capacity': {'lead_days': 15}})
    app = shell(demand, view='predict')
    button(app, '新建研判任务').click().run()
    assert not app.exception
    profile = demand.enterprises.list()[0]['id']
    app.selectbox(key='predict-new-profile').select(profile).run()
    app.text_area(key=f'predict-new-{profile}-request').input('预测节日丝巾')
    app.button(key=f'predict-new-{profile}-create').click().run()
    assert not app.exception
    wait(demand)
    app.run()
    assert not app.exception
    created = demand.demands.load(demand.demands.runs()[0]['id'])
    assert created['inputs']['path'] == 'trend' and created['inputs']['capacity'] == {'lead_days': 15}
    # The stub analysis asks a question, so the clarification dialog opens and nothing is confirmed yet.
    assert demand.llm.calls == ['DEMAND_ANALYZE']
    assert app.session_state['demand-dialog']['stage'] == 'questions'
    assert created['approved_revision'] is None


def test_predict_new_task_without_open_questions_is_confirmed(demand):
    import json
    previous = demand.llm
    class NoQuestions:
        configured, settings, calls = True, previous.settings, previous.calls
        async def __call__(self, prompt, **kwargs):
            value = json.loads(await previous(prompt, **kwargs))
            value['questions'] = []
            return json.dumps(value, ensure_ascii=False)
    demand.llm = NoQuestions()
    app = shell(demand, view='predict')
    button(app, '新建研判任务').click().run()
    app.text_area(key='predict-new-None-request').input('预测节日丝巾')
    app.text_input(key='predict-new-None-industry').input('丝绸')
    app.text_area(key='predict-new-None-capabilities').input('真丝丝巾')
    app.button(key='predict-new-None-create').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    created = demand.demands.load(demand.demands.runs()[0]['id'])
    assert created['approved_revision'] == created['revision']
    assert 'demand-dialog' not in app.session_state


def test_custom_chat_creates_task_answers_and_confirms(demand):
    app = shell(demand, view='custom')
    assert not app.exception
    app.button(key='custom-pick-alumni').click().run()
    next(t for t in app.text_input if t.label == '行业（必填）').input('丝绸')
    next(t for t in app.text_area if t.label == '生产能力（必填）').input('真丝丝巾与礼盒')
    app.chat_input(key='custom-chat-input').set_value('我要做校友纪念类产品').run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.runs()[0]
    assert run['inputs']['template'] == 'alumni' and run['inputs']['audience'] == '返校校友'
    assert any('已识别' in m.value for m in app.markdown)
    # The stub analysis asks one free-text question.
    app.chat_input(key='custom-chat-input').set_value('预算中档').run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert any('预算中档' in a for a in run['summaries'][-1]['answers'].values())
    confirm = next(b for b in app.button if b.label == '确认需求并继续')
    confirm.click().run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert run['approved_revision'] == run['revision']


def test_custom_designer_picks_brief_and_three_variants(demand):
    demand.llm = V4Model(demand.llm)
    culture(demand)
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    run, _ = start(demand, run, 'candidates')
    round_id = run['candidate_rounds'][0]['id']
    demand.demands.select_candidate(run['id'], round_id, 'C1')
    app = shell(demand, view='custom', demand=run['id'], step='方向与元素')
    assert not app.exception
    key = f"custom-pick-{run['id']}-{round_id}-C1-"
    app.pills(key=key + 'motifs').set_value(['M1']).run()
    button(app, '保存需求').click().run()
    assert demand.demands.load(run['id'])['element_picks']['adopt'] == ['M1']
    demand.llm.refs['brief'] = 'M1'
    button(app, '生成设计说明').click().run()
    wait(demand)
    run = demand.demands.load(run['id'])
    assert run['briefs'][0]['content']['story_mapping'][0]['element_ref'] == 'M1'
    app = shell(demand, view='custom', demand=run['id'], step='方案确认')
    assert not app.exception
    assert app.selectbox(key=f"demand-design-variants-{run['id']}").value == 3
    button(app, '生成设计方案').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    run = demand.demands.load(run['id'])
    assert [v['variant'] for v in run['design_versions']] == [1, 2, 3]
    app.button(key='custom-choose-' + run['design_versions'][1]['id']).click().run()
    assert demand.demands.load(run['id'])['design_choice'] == run['design_versions'][1]['id']


def test_pattern_page_empty_state_and_enterprise_settings(demand):
    app = shell(demand, view='pattern')
    assert not app.exception
    assert any('还没有纹样初稿' in i.value for i in app.info)
    # The shared fixture engine has no embedding settings, so render the enterprise tab on its own.
    app = AppTest.from_string('''
import streamlit as st
from lab.workbench_ui import enterprise_settings
enterprise_settings(st.session_state.test_engine)
''')
    app.session_state['test_engine'] = demand.engine
    app.run(timeout=20)
    assert not app.exception
    next(t for t in app.text_input if t.label == '企业名称').input('南浔某厂')
    next(t for t in app.text_input if t.label == '行业').input('丝绸')
    next(t for t in app.text_area if t.label == '生产能力').input('真丝')
    button(app, '保存档案').click().run()
    assert not app.exception
    assert demand.enterprises.list()[0]['name'] == '南浔某厂'
