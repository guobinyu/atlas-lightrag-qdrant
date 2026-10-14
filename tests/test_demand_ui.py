"""Exercise the real page with persistent services and a deterministic model."""
from streamlit.testing.v1 import AppTest

from test_demands import demand, work, INPUT, selected, start


def page(work):
    app = AppTest.from_string('''
import streamlit as st
from lab.demand_ui import demand_page
demand_page(st.session_state.test_engine)
''')
    app.session_state['test_engine'] = work.engine
    return app.run(timeout=15)


def button(app, label):
    return next(b for b in app.button if b.label == label)


def wait(work):
    # Queue behind the worker task without opening a second UI task.
    import time
    deadline = time.monotonic() + 5
    while work.active is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert work.active is None


def test_create_edit_continue_generate_and_export(demand):
    app = page(demand)
    assert not app.exception
    for field, value in INPUT.items():
        key = 'demand-new-' + field
        if field in ('path', 'days'):
            app.selectbox(key=key).select(value)
        elif field in ('request', 'capabilities', 'constraints'):
            app.text_area(key=key).input(value)
        else:
            app.text_input(key=key).input(value)
    button(app, '保存新任务').click().run()
    assert not app.exception
    run = demand.demands.runs()[0]
    assert run['inputs'] == INPUT
    # Fresh new-task form must not silently reuse the previous merchant.
    assert app.text_area(key='demand-new-capabilities').value == ''
    button(app, '开始完善需求').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert any(c.label == '暂不确定，请给建议' for c in app.checkbox)
    app.checkbox[0].check()
    button(app, '提交本轮回答').click().run()
    wait(demand)
    app.run()
    button(app, '按当前需求继续').click().run()
    button(app, '确认需求并继续').click().run()
    assert not app.exception
    assert not button(app, '生成新一轮产品候选').disabled
    button(app, '生成新一轮产品候选').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    button(app, '选为设计方向').click().run()
    button(app, '生成设计需求单').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert len(app.get('download_button')) == 2
    assert any('品牌名称：未提供' in m.value for m in app.markdown)
    next(t for t in app.text_area if t.label == '需求单修改意见').input('调整为简约风格')
    button(app, '修订为新版本').click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert len(demand.demands.load(run['id'])['briefs']) == 2


def test_old_brief_stays_readable_after_upstream_edit(demand):
    run = selected(demand)
    run, _ = start(demand, run, 'brief')
    demand.demands.update(run['id'], {**INPUT, 'audience': '新婚家庭'})
    app = page(demand)
    assert not app.exception
    assert any('历史条件' in w.value for w in app.warning)
    assert button(app, '修订为新版本').disabled
    assert len(app.get('download_button')) == 2


def test_copy_merchant_is_explicit_and_does_not_copy_request(demand):
    run = selected(demand)
    app = page(demand)
    app.selectbox(key='demand-copy-source').select(run['id']).run()
    button(app, '复制所选商家信息').click().run()
    assert not app.exception
    assert app.text_area(key='demand-new-capabilities').value == INPUT['capabilities']
    assert app.text_area(key='demand-new-request').value == ''
    assert app.text_input(key='demand-new-audience').value == ''


def test_structured_wizard_resume_and_confirmation(demand):
    run, _ = start(demand, demand.demands.create(INPUT), 'analyze')
    analysis = run['clarifications'][-1]
    analysis['questions'] = [{'text': '产品优先方向？', 'options': ['丝巾', '组合系列']}, {'text': '还有什么要求？', 'options': []}]
    analysis['suggestions'] = {'scene': '节日礼赠'}
    demand.demands.save(run)
    app = page(demand)
    button(app, '继续完善需求').click().run()
    assert not app.exception
    assert app.radio[0].value is None
    button(app, '保存并下一题').click().run()
    assert any('请选择' in e.value for e in app.error)
    app.radio[0].set_value('组合系列')
    next(t for t in app.text_area if t.label == '补充说明／其他回答').input('统一纹样')
    button(app, '保存并下一题').click().run()
    assert not app.exception
    assert demand.demands.load(run['id'])['revision'] == 1
    # A new browser session resumes persisted cursor and answers without a new model call.
    app = page(demand)
    assert not any(t.label == '补充说明／其他回答' for t in app.text_area)
    button(app, '继续完善需求').click().run()
    assert any('第 2 / 2 题' in c.value for c in app.caption)
    button(app, '上一题').click().run()
    assert app.radio[0].value == '组合系列'
    assert next(t for t in app.text_area if t.label == '补充说明／其他回答').value == '统一纹样'
    # Confirming can keep already saved drafts without forcing the remaining question.
    next(b for b in reversed(list(app.button)) if b.label == '按当前需求继续').click().run()
    assert not app.exception
    assert next(t for t in app.text_input if t.label == '品牌名称（选填）' and t.key.startswith('demand-confirm')).value == ''
    assert next(t for t in app.text_input if t.label == '使用场景' and t.key.startswith('demand-confirm')).value == INPUT['scene']
    button(app, '确认需求并继续').click().run()
    assert not app.exception
    result = demand.demands.load(run['id'])
    assert result['approved_revision'] == 2
    assert result['summaries'][-1]['answers']['产品优先方向？'] == '组合系列；统一纹样'
    assert demand.llm.calls == ['DEMAND_ANALYZE']


def test_legacy_over_limit_opens_confirmation(demand):
    run = demand.demands.create(INPUT)
    run['clarifications'] = [{'id': str(i), 'revision': 1, 'known': [], 'unknown': ['预算'],
                              'questions': [f'问题{i}']} for i in range(6)]
    demand.demands.save(run)
    app = page(demand)
    button(app, '查看并确认需求').click().run()
    assert not app.exception
    assert any('最多 5 轮' in x.value for x in app.info)
    assert not any(t.label == '补充说明／其他回答' for t in app.text_area)
    button(app, '确认需求并继续').click().run()
    assert not app.exception
    assert not demand.llm.calls


def test_candidate_failure_feedback_and_retry_stay_in_step(demand):
    run = demand.demands.create(INPUT)
    demand.demands.approve(run['id'])
    demand.llm.bad = 'DEMAND_CANDIDATES'
    run, status = start(demand, run, 'candidates')
    assert status['status'] == 'failed'
    app = page(demand)
    assert not app.exception
    retry = button(app, '重试失败阶段')
    assert retry.key.startswith('demand-retry-candidates-')
    assert any('href="#demand-candidates"' in m.value for m in app.markdown)
    demand.llm.bad = None
    retry.click().run()
    wait(demand)
    app.run()
    assert not app.exception
    assert any('产品候选已完成' in x.value for x in app.success)
    assert len(demand.demands.load(run['id'])['candidate_rounds']) == 1


def test_running_analysis_keeps_waiting_dialog_and_close_does_not_reopen(demand):
    import threading
    demand.demands.create(INPUT)
    demand.llm.started, demand.llm.release = threading.Event(), threading.Event()
    app = page(demand)
    try:
        button(app, '开始完善需求').click().run()
        assert not app.exception
        assert app.session_state['demand-dialog']['stage'] == 'waiting'
        assert any('正在整理需求' in x.value for x in app.info)
        # Simulate the on_dismiss callback through a fresh AppTest script session.
        del app.session_state['demand-dialog']
        del app.session_state['demand-await']
    finally:
        demand.llm.release.set()
        wait(demand)
    app.run()
    assert not app.exception
    assert 'demand-dialog' not in app.session_state
    assert len(demand.llm.calls) == 1
    button(app, '继续完善需求').click().run()
    assert not app.exception
    assert any(t.label == '补充说明／其他回答' for t in app.text_area)
