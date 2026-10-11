"""Isolated browser acceptance harness: real Engine, fixed synthetic LLM output.

Run with Streamlit. Never opens the user's data stores; temporary directory only.
"""
from pathlib import Path
import sys
import tempfile
import atexit
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
from lab.config import Settings, ROOT
from lab.engine import Engine
from lab.llm import LLMSettings
from lab.ontology.profile import uri
from lab.workbench_ui import navigation, render
import lab.demo as automatic

TEXT='【合成验收，非历史事实】张甲在甲村实施了本次织造，使用丝线制作了这件织锦样品。该资料仅用于界面测试。李乙实施了本次制茶。'

@st.cache_resource
def fixture():
    directory=Path(tempfile.mkdtemp(prefix='atlas-semantic-browser-'))
    (directory/'models').symlink_to(ROOT/'data/models',target_is_directory=True)
    engine=Engine(Settings(data_dir=directory, workspace='semantic_browser',seed_demo=False))
    engine.ready.result(timeout=180)
    atexit.register(engine.close)
    engine.workbench.llm.settings=LLMSettings(model='固定响应：浏览器合成验收')
    async def fixed(cfg,messages):
        content = messages[-1]['content']
        if '网页检索词' in content:
            return '["宋朝文化 合成验收"]', {}
        if '\n陈述：' in content:
            claims = json.loads(content.split('\n陈述：', 1)[1].split('\n原文：', 1)[0])
            return json.dumps([{'id': c['id'], 'verdict': 'supported', 'reason': '合成验收：原文明确记载参与者'} for c in claims]), {}
        return json.dumps({'entities':[{'id':'a','label':'本次织造','type':'E12','concrete':True},{'id':'b','label':'张甲','type':'E21'},{'id':'c','label':'甲村','type':'E53'},
                                      {'id':'d','label':'本次制茶','type':'E7','concrete':True},{'id':'e','label':'李乙','type':'E21'}],
                           'statements':[{'subject':'a','predicate':str(uri('P14')),'object':'b','quote':TEXT,'start':0},
                                         {'subject':'b','predicate':str(uri('P7')),'object':'c','quote':TEXT,'start':0},
                                         {'subject':'d','predicate':str(uri('P14')),'object':'e','quote':'李乙实施了本次制茶。'}], 'deferred':[]},ensure_ascii=False),{}
    engine.workbench.llm._request=fixed
    engine.workbench.sources.add({'title':'合成浏览器验收资料','text':TEXT,'url':''})
    automatic.search_web = lambda query, limit: [{'title': '合成浏览器验收资料', 'url': 'https://example.org/synthetic'}]
    automatic.fetch_source = lambda url, title: {'title': title, 'url': url, 'text': TEXT, 'original_chars': len(TEXT), 'kind': 'text'}
    return engine

st.set_page_config(layout='wide')
st.markdown((ROOT / 'assets/style.css').read_text(encoding='utf-8'), unsafe_allow_html=True)
st.warning('隔离验收：临时数据库、合成资料、固定模型响应；不是模型质量评估。')
engine=fixture()
render(engine, navigation())
