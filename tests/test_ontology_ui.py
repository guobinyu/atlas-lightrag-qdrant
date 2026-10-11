"""UI regression for review selection and service-enforced publish boundary."""
import json
from streamlit.testing.v1 import AppTest
from lab.ontology.service import SemanticService
from lab.ontology.profile import uri
from evidence_helpers import checked
from lab.ontology.ui import directed_figure, graph_focus


def test_review_page_confirmation_and_publication(tmp_path):
    service=SemanticService(tmp_path)
    text='合成验收：张甲实施了本次织造。'
    bid=service.create_batch([{'id':'source','text':text,'title':'合成验收'}], {'model':'fixed'}, 'silk')
    block=service.report(bid)[0]
    payload={'entities':[{'id':'a','label':'本次织造','type':'E12','concrete':True},{'id':'b','label':'张甲','type':'E21'}],
             'statements':[{'subject':'a','predicate':str(uri('P14')),'object':'b','quote':text,'start':0}], 'deferred':[]}
    service.save_response(block['id'],json.dumps(payload), verification=checked(payload,text,{'model':'fixed'}))
    script=f'''
from pathlib import Path
from types import SimpleNamespace
from lab.ontology.service import SemanticService
from lab.ontology.ui import semantic_page
service=SemanticService(Path({str(tmp_path)!r}))
work=SimpleNamespace(semantic=service, jobs=lambda:[], active=None, sources=SimpleNamespace(list=lambda:[]),llm=SimpleNamespace(settings=SimpleNamespace(model='fixed')))
semantic_page(SimpleNamespace(workbench=work))
'''
    app=AppTest.from_string(script).run()
    assert not app.exception
    selector = next(s for s in app.selectbox if s.label == '抽取批次')
    assert any('合成验收' in option and '1份资料' in option and bid[:8] in option for option in selector.options)
    selector.select(bid).run()
    assert not app.exception
    assert any(bid in caption.value for caption in app.caption)
    assert not any('批准' in b.label for b in app.button)
    assert not any('我已逐条' in c.label for c in app.checkbox)
    assert len(service.get_published_graph()['statements'])==1
    assert len(app.get('plotly_chart')) == 1
    assert not any(s.label == '查看抽取陈述' for s in app.selectbox)
    app.session_state['semantic-tabs'] = '抽取结果'
    app.run()
    assert not app.exception
    assert len(app.get('plotly_chart')) == 0
    assert any(s.label == '查看抽取陈述' for s in app.selectbox)
    app.session_state['semantic-tabs'] = '已发布图谱'
    app.run()
    assert not app.exception
    assert len(app.get('plotly_chart')) == 1
    next(b for b in app.button if b.label=='撤回当前陈述').click().run()
    assert not app.exception
    assert not service.get_published_graph()['statements']


def test_graph_click_resolves_ids_direction_predicate_and_multiple_sources():
    a = {'id': 'activity', 'label': '同名', 'type': 'E7'}
    b = {'id': 'person', 'label': '同名', 'type': 'E21'}
    c = {'id': 'place', 'label': '甲村', 'type': 'E53'}
    claims = [
        {'id': 'one', 'subject': a, 'object': b, 'predicate': str(uri('P14'))},
        {'id': 'two', 'subject': a, 'object': c, 'predicate': str(uri('P7'))},
        {'id': 'another-source', 'subject': a, 'object': b, 'predicate': str(uri('P14'))},
    ]
    node = graph_focus(claims, [{'customdata': ['node', 'person']}])
    assert node['ids'] == ['one', 'another-source']
    assert graph_focus(claims, [{'customdata': ['node', 'place']}])['ids'] == ['two']
    edge = graph_focus(claims, [{'customdata': ['edge', 'activity', 'person', str(uri('P14'))]}])
    assert edge['ids'] == ['one', 'another-source']
    assert graph_focus(claims, [{'customdata': ['edge', 'person', 'activity', str(uri('P14'))]}]) is None
    assert graph_focus(claims, [{'customdata': ['node', 'stale-id']}]) is None
    assert graph_focus(claims, []) is None
    figure = directed_figure(claims, selected_node='person')
    assert ['node', 'person'] in figure.data[-1].customdata
    assert 'customdata}' not in figure.data[-1].hovertemplate
    assert figure.layout.clickmode == 'event+select'
    assert any(trace.customdata and trace.customdata[0][0] == 'edge' for trace in figure.data[:-1])
