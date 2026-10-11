"""Run: .venv/bin/python -m streamlit run demo.py --server.port 8502"""
import asyncio

import streamlit as st

from lab.config import ROOT, load_settings
from lab.demo import build_demo
from lab.llm import ExtractionLLM, LLMSettings
from lab.ontology.service import SemanticService
from lab.ontology.ui import directed_figure, title
from lab.sources import SourceStore

st.set_page_config(page_title='文化知识图谱 Demo', page_icon='◈', layout='wide')
st.markdown((ROOT / 'assets/style.css').read_text(encoding='utf-8'), unsafe_allow_html=True)
settings = load_settings()
directory = settings.data_dir / 'ontology-demo'
directory.mkdir(parents=True, exist_ok=True)
llm = ExtractionLLM(directory / 'llm-settings.json', lambda *args: None)
# Read the Atlas connection if the standalone demo has not saved its own settings.
if not llm.configured:
    llm.settings = ExtractionLLM(settings.data_dir / 'workbench' / settings.workspace / 'llm-settings.json', lambda *args: None).settings
semantic = SemanticService(directory / 'ontology', recover=False)
sources = SourceStore(directory / 'sources')

st.title('文化知识图谱 Demo')
st.write('输入主题 → 模型规划检索词 → 搜集网页正文 → CIDOC CRM 抽取与校验 → 自动发布图谱')
st.caption('默认 3 份，最多 10 份；每份使用前 9,000 字符。不需要 Embedding、Qdrant 或人工审核。数据保存于独立 demo 目录。')
with st.expander('模型连接', expanded=not llm.configured):
    with st.form('demo-model'):
        endpoint = st.text_input('完整接口地址', llm.settings.endpoint)
        model = st.text_input('模型名称', llm.settings.model)
        key = st.text_input('API Key（留空保留当前接口的 Key）', type='password')
        if st.form_submit_button('保存连接'):
            try:
                retained = llm.settings.api_key if endpoint.strip() == llm.settings.endpoint else ''
                llm.save(LLMSettings('http', endpoint.strip(), model.strip(), key or retained, 180))
                st.success('已保存')
            except ValueError as exc:
                st.error(str(exc))
with st.form('demo-task'):
    topic = st.text_input('研究主题', '宋朝文化', max_chars=300)
    count = st.number_input('希望读取的资料数量', 1, 10, 3)
    submitted = st.form_submit_button('一键搜集并构建图谱', type='primary', disabled=not llm.configured)
st.caption(f'模型：{llm.settings.model or "未配置"}。网页正文将发送至已配置的模型服务。搜索相关性由检索词决定，demo 未增加独立相关性评分。')
if submitted:
    progress = st.empty()
    events = []
    def emit(event, data):
        events.append({'event': event, 'data': data})
        progress.info(event + ' · ' + str(data.get('title', data.get('query', ''))))
    llm.emit = emit
    try:
        with st.spinner('正在执行，完成前请保持页面开启…'):
            result = asyncio.run(build_demo(topic, int(count), llm, sources, semantic, emit))
        st.session_state.demo_result = result
        st.session_state.demo_events = events
        progress.empty()
    except Exception as exc:
        progress.error(f'{type(exc).__name__}：任务失败，请检查模型连接。')
result = st.session_state.get('demo_result')
if result:
    st.write(f"目标 {result['requested']} 份，实际读取 {result['collected']} 份；自动发布 {result['published']} 条，校验未通过 {result['blocked']} 条。")
    if result['collected'] < result['requested']:
        st.warning('可读取资料不足目标数量。可换检索词重试，失败网站已记录。')
    if result['status'] != 'success':
        st.caption('本次存在不足或失败项，请查看执行记录和校验结果。')
    with st.expander('执行记录'):
        st.json(st.session_state.get('demo_events', []))
published = semantic.get_published_graph()['statements']
if published:
    st.plotly_chart(directed_figure(published[:80]), width='stretch')
    st.caption(f'已发布 {len(published)} 条陈述，图中最多展示 80 条。自动发布结果需结合来源判断。')
    for claim in published:
        with st.expander(title(claim)):
            st.text(claim['evidence']['quote'])
            if claim.get('source', {}).get('url'):
                st.link_button('原始来源', claim['source']['url'])
    for fmt, filename, mime in [('turtle', 'graph.ttl', 'text/turtle'), ('json-ld', 'graph.jsonld', 'application/ld+json'), ('json', 'audit.json', 'application/json')]:
        st.download_button('导出 ' + fmt, semantic.export(fmt), filename, mime, key='demo-export-' + fmt)
else:
    st.info('暂无已发布陈述。配置模型后开始构建；若未生成关系，请检查下方校验结果。')
with st.expander('校验结果与失败文本块'):
    st.json(semantic.report())
    st.json([{'statement': title(s), 'errors': s['validation']['errors']} for s in semantic.list_candidates(status='pending')])
