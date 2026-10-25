"""Culture page: browse, filter, annotate and export through the service boundary."""
import asyncio
from concurrent.futures import Future
import json

from streamlit.testing.v1 import AppTest

from test_culture import decoded, FakeIndex


def app(tmp_path):
    service, _ = decoded(tmp_path)
    index = FakeIndex()
    script = f'''
import asyncio
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import streamlit as st
from lab.culture.service import CultureService
from lab.culture.ui import culture_page
from test_culture import FakeIndex
service = CultureService(Path({str(tmp_path)!r}))
index = st.session_state.setdefault('index', FakeIndex())
def done(coro):
    f = Future(); f.set_result(asyncio.run(coro)); return f
engine = SimpleNamespace(
    culture_search=lambda q, kinds=None, themes=None: done(service.search(index, q, kinds=kinds, themes=themes)),
    culture_sync=lambda: done(service.sync(index)),
    workbench=SimpleNamespace(culture=service, active=None, jobs=lambda: [],
                              sources=SimpleNamespace(list=lambda: []), llm=SimpleNamespace(configured=False)))
culture_page(engine)
'''
    return service, AppTest.from_string(script).run(timeout=15)


def test_browse_filter_search_and_reject(tmp_path):
    service, page = app(tmp_path)
    assert not page.exception
    assert [m.value for m in page.metric] == ['4', '1', '1', '1']
    titles = [e.label for e in page.expander]
    assert '配色方案：正红金色' in titles and '符号纹样：牡丹纹' in titles
    assert any('#C8102E' in m.value for m in page.markdown)
    next(s for s in page.multiselect if s.label == '素材类型').set_value(['palette']).run()
    assert [e.label for e in page.expander if '：' in e.label] == ['配色方案：正红金色']
    next(s for s in page.multiselect if s.label == '素材类型').set_value([]).run()
    next(t for t in page.text_input if t.label == '语义检索').input('牡丹')
    next(b for b in page.button if b.label == '检索').click().run()
    assert not page.exception
    assert any('牡丹纹' in e.label and '相似度' in e.label for e in page.expander)
    next(b for b in page.button if b.label == '清除检索结果').click().run()
    motif = next(i for i in service.elements() if i['label'] == '牡丹纹')
    next(b for b in page.button if b.key == 'culture-reject-' + motif['id']).click().run()
    assert motif['id'] not in {i['id'] for i in service.elements()}
    assert not any(b.label == '解码所选资料' and not b.disabled for b in page.button)


def test_annotate_palette_and_export(tmp_path):
    service, page = app(tmp_path)
    form = next(t for t in page.text_input if t.label == '名称')
    next(s for s in page.selectbox if s.label == '类型').set_value('palette')
    form.input('青绿山水')
    next(t for t in page.text_area if t.label.startswith('配色')).input('石青,#1F5F8B,main\n石绿,#3A7D44,secondary')
    next(b for b in page.button if b.label == '保存素材').click().run()
    assert not page.exception
    assert any(s.value == '素材已保存。' for s in page.success)
    saved = next(i for i in service.elements() if i['label'] == '青绿山水')
    assert saved['origin'] == 'manual' and saved['colors'][1]['hex'] == '#3A7D44'
    next(t for t in page.text_area if t.label.startswith('配色')).input('石青,1F5F8B,main')
    next(t for t in page.text_input if t.label == '名称').input('坏配色')
    next(b for b in page.button if b.label == '保存素材').click().run()
    assert any('HEX' in e.value for e in page.error)
    exported = json.loads(service.export('json'))
    assert '青绿山水' in {e['label'] for e in exported['elements']}
    assert len([b for b in page.get('download_button')]) == 2
