from copy import deepcopy
import json

import pytest
from campaign_fixture import content_plan
from test_campaigns import work, start, INPUTS
from lab.campaign_content import constraints, content_errors, clean_plan, display_plan, markdown
from lab.campaigns import export_markdown


def test_defaults_and_explicit_revision_conditions():
    run = {'inputs': {'request': '搜索最近30天热点'}}
    assert constraints(run, {}) == {'duration_days': 7, 'budget_total': 30000}
    run['inputs'].update(request='搜近期热点，活动周期两周，预算五万元', budget='2万元')
    assert constraints(run, {}) == {'duration_days': 14, 'budget_total': 20000}
    assert constraints(run, {'revision': '预算控制在1万元，周期3天'}) == {'duration_days': 3, 'budget_total': 10000}
    base = {'plan': content_plan(4, 8000)}
    assert constraints(run, {'revision': '修改标题'}, base) == {'duration_days': 4, 'budget_total': 8000}


def test_complete_content_and_readable_export():
    plan = content_plan()
    assert not content_errors(plan, {'duration_days':7, 'budget_total':30000})
    rendered = markdown(plan)
    assert '行动转化' in rendered and '预算分配' in rendered and '台词' in rendered
    assert '【判断】' not in rendered
    plan['strategy']['opportunity'] = '【判断】根据[S3]与I3/K3，提炼生活方式切口'
    cleaned = clean_plan(plan)
    assert 'S3' not in markdown(cleaned) and 'I3/K3' not in markdown(cleaned)


@pytest.mark.parametrize('field,broken', [
    ('posts', []), ('video', {}), ('schedule', []), ('budget', []), ('metrics', []),
    ('strategy', {}), ('overview', None), ('posts', [{'id': []}]), ('assumptions', None), ('title', None),
])
def test_missing_content_is_incomplete_not_crash(field, broken):
    value=content_plan(); value[field]=broken
    assert content_errors(value, {'duration_days':7,'budget_total':30000})
    markdown(display_plan(value))


def test_schedule_budget_and_deliverables_checked():
    value=content_plan(); value['budget'][0]['amount']=29000
    value['schedule'][0].update(day=8, content_ids=['missing'])
    errors=content_errors(value, {'duration_days':7,'budget_total':30000})
    assert any('金额之和' in e for e in errors)
    assert any('超出' in e for e in errors)
    assert any('不存在' in e for e in errors)
    value=content_plan(); value['posts'][0]['body']='发布一篇穿搭图文'
    assert any('完整正文' in e for e in content_errors(value, {'duration_days':7,'budget_total':30000}))


def test_demo_repair_only_once_and_revision_keeps_draft(work):
    original=work.llm
    class Model:
        configured=True
        settings=original.settings
        async def __call__(self, prompt, **kwargs):
            result=await original(prompt, **kwargs)
            if prompt.startswith('PLAN'):
                data=json.loads(result); data['posts']=[]; return json.dumps(data)
            return result
    work.llm=Model()
    result, run=start(work, inputs={**INPUTS, 'mode':'demo'})
    assert result['status']=='failed'
    assert original.calls.count('PLAN')==2
    draft=deepcopy(run['versions'][0]); assert draft['status']=='draft'
    work.llm=original
    result, run=start(work,'campaign_revise',{'run_id':run['id'],'topic_id':'T1','version_id':draft['id'],'revision':'补齐图文','mode':'demo'})
    assert result['status']=='success' and run['versions'][0]==draft
    assert run['versions'][1]['status']=='complete'


def test_upgrade_legacy_and_revision_apply_budget_without_research(work):
    _, run=start(work)
    old=deepcopy(run['versions'][0]); before=len(work.llm.calls)
    result, run=start(work,'campaign_revise',{'run_id':run['id'],'topic_id':'T1','version_id':old['id'],'revision':'升级新版，预算1万元，周期3天','mode':'demo'})
    assert result['status']=='success'
    new=run['versions'][-1]
    assert run['versions'][0]==old and new['plan']['content_version']==2
    assert new['plan']['overview']['budget_total']==10000
    assert new['plan']['overview']['duration_days']==3
    assert 'SEARCH' not in work.llm.calls[before:]
    exported=export_markdown(run,new)
    assert '附录：来源' in exported and 'https://example.com/news' in exported
    assert '"text":' not in exported


def test_ui_v2_deliverables_and_draft(work):
    from streamlit.testing.v1 import AppTest
    _, run=start(work, inputs={**INPUTS,'mode':'demo'})
    app=AppTest.from_string('''
import streamlit as st
from lab.campaign_ui import campaign_page
campaign_page(st.session_state.engine)
''')
    app.session_state.engine=work.engine; app.run(timeout=20)
    assert not app.exception
    assert [t.label for t in app.tabs]==['营销方案','执行安排','推荐依据']
    assert len(app.code)==3 and len(app.table)==3
    assert any(s.value=='方案已生成' for s in app.success)
    assert len(app.get('download_button'))==2
    run['versions'][0]['status']='draft'; run['versions'][0]['errors']=['缺少图文']; run['versions'][0]['plan']['posts']=None
    work.campaigns.save(run); app.run(timeout=20)
    assert not app.exception
    assert any(b.label=='继续完善' for b in app.button)


def test_partial_generation_survives_repair_connection_failure(work):
    original = work.llm
    class Model:
        configured = True
        settings = original.settings
        calls = 0
        async def __call__(self, prompt, **kwargs):
            if prompt.startswith('PLAN'):
                self.calls += 1
                if self.calls == 2:
                    raise ConnectionError('repair unavailable')
                value = json.loads(await original(prompt, **kwargs))
                value['posts'] = []
                return json.dumps(value)
            return await original(prompt, **kwargs)
    work.llm = Model()
    result, run = start(work, inputs={**INPUTS, 'mode': 'demo'})
    assert result['status'] == 'failed'
    draft = deepcopy(run['versions'][0])
    assert draft['status'] == 'draft' and draft['plan']['strategy']
    assert not work.engine.gate.locked()
    work.llm = original
    result, run = start(work, 'campaign_retry', {'run_id': run['id'], 'operation_id': result['operation_id']})
    assert result['status'] == 'success'
    assert run['versions'][0] == draft and run['versions'][-1]['status'] == 'complete'
