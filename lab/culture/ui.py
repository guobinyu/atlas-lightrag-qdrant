"""Culture library page: talks to CultureService and engine task helpers, never to SQLite."""
from datetime import datetime
from html import escape
import re
from zoneinfo import ZoneInfo

import streamlit as st

from .prompts import TYPES, BASES, PREDICATES, ROLES

ROLE = {'main': '主色', 'secondary': '辅助色', 'accent': '点缀色'}
STATUS = {'success': '已完成', 'partial': '部分完成', 'failed': '失败', 'interrupted': '中断', 'running': '执行中'}
SHOWN = 100


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return '时间未知'


def swatches(colors):
    # HEX values are validated on write; names are escaped. Text labels carry the meaning, not the color alone.
    box = 'display:inline-block;width:14px;height:14px;border-radius:3px;border:1px solid #c9d2cc;vertical-align:-2px;margin-right:4px;background:'
    return '　'.join(f'<span style="{box}{c["hex"]}" aria-hidden="true"></span>{escape(c["name"])} <code>{c["hex"]}</code>（{ROLE[c["role"]]}）'
                    for c in colors)


def parse_colors(text):
    colors = []
    for line in filter(None, (l.strip() for l in text.splitlines())):
        parts = [x.strip() for x in re.split(r'[,，]', line)]
        if len(parts) != 3 or parts[2] not in ROLES:
            raise ValueError('配色每行格式为：名称,#HEX,main/secondary/accent')
        colors.append({'name': parts[0], 'hex': parts[1], 'role': parts[2]})
    return colors


def sync_index(engine):
    try:
        engine.culture_sync().result(timeout=120)
    except ValueError:
        st.caption('当前有任务执行，向量索引将在下次检索或解码完成时同步。')


def element_view(service, item, busy):
    origin = '人工标注' if item.get('origin') == 'manual' else '模型解码'
    st.caption(f"{TYPES[item['type']]} · {BASES[item['basis']]} · {origin}" + (' · 已驳回' if item['status'] == 'rejected' else ''))
    if item['basis'] == 'interpretation':
        st.caption('设计解读：可作为创意依据，不代表史实。')
    if item['description']:
        st.write(item['description'])
    if item['colors']:
        st.markdown(swatches(item['colors']), unsafe_allow_html=True)
    if item['themes']:
        st.caption('主题：' + '、'.join(item['themes']))
    for r in item['relations']:
        st.write(f"{r['label']} → {r['other']}" if r['direction'] == 'out' else f"{r['other']} → {r['label']} → 本素材")
    if item['evidence']:
        st.markdown('**原文依据**')
        for e in item['evidence']:
            st.text(e['quote'])
            st.caption('样本：' + (e['sample'] or e['sample_id']) + ('　·　近似定位，请对照原文' if e['match_method'] != 'exact' else ''))
    else:
        st.caption('人工标注，暂无原文依据。')
    if item['status'] == 'published':
        if st.button('驳回素材', key='culture-reject-' + item['id'], disabled=busy):
            service.set_status(item['id'], 'rejected')
            st.rerun()
    elif st.button('恢复素材', key='culture-restore-' + item['id'], disabled=busy):
        service.set_status(item['id'], 'published')
        st.rerun()


def browse(engine, service, busy):
    themes = service.themes()
    published = service.elements()
    overview = [{'主题层': t, '素材数': sum(t in i['themes'] for i in published),
                 '样本数': len({s['id'] for i in published if t in i['themes'] for s in i['samples']})} for t in themes]
    overview.append({'主题层': '未分主题', '素材数': sum(not i['themes'] for i in published),
                     '样本数': len({s['id'] for i in published if not i['themes'] for s in i['samples']})})
    with st.expander('主题层概览', expanded=False):
        st.dataframe(overview, hide_index=True, width='stretch')
    samples = {s['id']: s for s in service.samples()}
    a, b, c = st.columns(3)
    kinds = a.multiselect('素材类型', list(TYPES), format_func=TYPES.get, key='culture-kinds')
    theme = b.selectbox('主题', ['', *themes, '未分主题'], format_func=lambda t: t or '全部主题', key='culture-theme')
    sample = c.selectbox('样本层', ['', *samples], format_func=lambda k: f"{samples[k]['title']}（{samples[k]['elements']}）" if k else '全部样本', key='culture-sample')
    rejected = st.toggle('包含已驳回素材', key='culture-rejected')
    with st.form('culture-search', border=False):
        left, right = st.columns([4, 1], vertical_alignment='bottom')
        query = left.text_input('语义检索', placeholder='例如：婚礼 丝巾 吉祥纹样', max_chars=500)
        search = right.form_submit_button('检索', width='stretch')
    if search and query.strip():
        try:
            hits = engine.culture_search(query, kinds or None, [theme] if theme and theme != '未分主题' else None).result(timeout=120)
            st.session_state['culture-hits'] = {'query': query, 'items': [(item['id'], score) for item, score in hits]}
        except ValueError as exc:
            st.error(str(exc))
    hits = st.session_state.get('culture-hits')
    if hits:
        st.caption(f"「{hits['query']}」的检索结果 · 相似度为向量距离，不是事实置信度")
        if st.button('清除检索结果', key='culture-clear-search'):
            st.session_state.pop('culture-hits')
            st.rerun()
        items = [(service.detail(eid), score) for eid, score in hits['items']]
        items = [(item, score) for item, score in items if item]
        if not items:
            st.info('没有匹配的已发布素材。')
    else:
        values = service.elements(status=None if rejected else 'published', kinds=kinds, theme=theme or None, sample_id=sample or None)
        items = [(item, None) for item in values]
        if not items:
            st.info('当前筛选下没有素材。可以在「解码资料」中解码已有资料，或在「人工标注」中手动添加。')
        elif len(items) > SHOWN:
            st.caption(f'共 {len(items)} 项，显示前 {SHOWN} 项，请用筛选或检索缩小范围。')
    for item, score in items[:SHOWN]:
        title = f"{TYPES[item['type']]}：{item['label']}" + (f'　·　相似度 {score:.2f}' if score is not None else '')
        with st.expander(title):
            element_view(service, item, busy)


def decode(engine, service, busy):
    from lab.workbench_ui import start_job
    work = engine.workbench
    configured = work.llm.configured
    with st.form('culture-auto'):
        topic = st.text_input('采集主题', '婚礼 丝绸 吉祥纹样 寓意', max_chars=300)
        limit = st.number_input('希望读取的资料数量', 1, 10, 3)
        automatic = st.form_submit_button('一键采集并解码', type='primary', disabled=busy or not configured)
    if automatic:
        start_job(engine, 'culture_auto', {'topic': topic, 'limit': int(limit)})
    sources = {s['id']: s for s in work.sources.list()}
    picked = st.multiselect('或选择已保存资料（最多 10 份）', list(sources), format_func=lambda k: sources[k]['title'], max_selections=10, key='culture-sources')
    if st.button('解码所选资料', disabled=busy or not configured or not picked):
        start_job(engine, 'culture_decode', {'source_ids': picked})
    if not configured:
        st.info('请先在「模型设置」连接模型。浏览、检索和人工标注不需要模型。')
    st.caption('正文会发送到已配置模型。每份资料按约 3,000 字分块解码；引文能在原文定位的素材与关系才会入库。')
    for batch in service.batches()[:10]:
        name = batch['topic'] or (batch['sources'][0]['title'] if batch['sources'] else '未命名资料')
        with st.expander(f"{name} · {STATUS.get(batch['status'], batch['status'])} · {local_time(batch['created_at'])} · {len(batch['sources'])}份资料"):
            summary = service.summary(batch['id'])
            st.write(f"文本块 {summary['blocks']} · 未完成 {summary['failed_blocks']} · 素材 {summary['elements']} · 关系 {summary['relations']} · 未入库提示 {summary['issues']}")
            for block in service.report(batch['id']):
                if block['error']:
                    st.warning(block['error'])
                for issue in block['issues']:
                    st.caption(issue)
            if batch['status'] in {'partial', 'failed', 'interrupted'} and st.button('重试未完成文本块', key='culture-retry-' + batch['id'], disabled=busy or not configured):
                start_job(engine, 'culture_retry', {'batch_id': batch['id']})


def annotate(engine, service, busy):
    themes = service.themes()
    items = service.elements(status=None)
    options = {'': '新增素材', **{i['id']: f"{TYPES[i['type']]}：{i['label']}" for i in items}}
    target = st.selectbox('编辑对象', list(options), format_func=options.get, key='culture-edit-target')
    base = service.detail(target) if target else {}
    with st.form('culture-annotate-' + (target or 'new')):
        kind = st.selectbox('类型', list(TYPES), index=list(TYPES).index(base.get('type', 'motif')), format_func=TYPES.get)
        label = st.text_input('名称', base.get('label', ''), max_chars=60)
        description = st.text_area('描述', base.get('description', ''), max_chars=800)
        basis = st.radio('依据', list(BASES), index=list(BASES).index(base.get('basis', 'interpretation')), format_func=BASES.get, horizontal=True)
        picked = st.multiselect('主题', themes, default=[t for t in base.get('themes', []) if t in themes])
        colors = st.text_area('配色（仅配色方案填写；每行：名称,#HEX,main/secondary/accent）',
                              '\n'.join(f"{c['name']},{c['hex']},{c['role']}" for c in base.get('colors', [])))
        save = st.form_submit_button('保存素材', disabled=busy)
    st.caption('文化内涵始终按设计解读处理。人工描述不是原文依据，不能在方案中作为事实引用。')
    if save:
        try:
            value = {'type': kind, 'label': label, 'description': description, 'basis': basis, 'themes': picked,
                     'colors': parse_colors(colors) if kind == 'palette' else []}
            service.annotate(value, target or None)
            sync_index(engine)
            st.success('素材已保存。')
        except ValueError as exc:
            st.error(str(exc))
    published = {i['id']: options[i['id']] for i in items if i['status'] == 'published'}
    if len(published) >= 2:
        with st.form('culture-relate'):
            a, b, c = st.columns(3)
            subject = a.selectbox('主语素材', list(published), format_func=published.get)
            predicate = b.selectbox('关系', list(PREDICATES), format_func=lambda k: PREDICATES[k][0])
            obj = c.selectbox('宾语素材', list(published), format_func=published.get)
            relate = st.form_submit_button('添加关系', disabled=busy)
        if relate:
            try:
                service.relate(subject, predicate, obj)
                sync_index(engine)
                st.success('关系已添加。')
            except ValueError as exc:
                st.error(str(exc))
    with st.form('culture-theme-add'):
        name = st.text_input('新增主题', max_chars=20)
        add = st.form_submit_button('添加主题', disabled=busy)
    if add:
        try:
            service.add_theme(name)
            st.success('主题已添加，之后新建的解码批次会使用。')
        except ValueError as exc:
            st.error(str(exc))


def culture_page(engine):
    from lab.workbench_ui import job_panel, header
    work = engine.workbench
    service = work.culture
    busy = work.active is not None
    header('文化知识库', '按主题、样本、素材三层解码文化资料，为产品候选、需求单和设计方案提供纹样、配色与叙事依据。', 'ATLAS / CULTURE LIBRARY')
    with st.expander('最近任务', expanded=busy):
        job_panel(engine)
    stats = service.stats()
    columns = st.columns(4)
    for column, (name, value) in zip(columns, [('已发布素材', stats['elements']), ('符号纹样', stats['by_type'].get('motif', 0)),
                                               ('配色方案', stats['by_type'].get('palette', 0)), ('样本资料', stats['samples'])]):
        column.metric(name, value)
    tab_browse, tab_decode, tab_annotate, tab_export = st.tabs(['素材浏览', '解码资料', '人工标注', '导出数据集'])
    with tab_browse:
        browse(engine, service, busy)
    with tab_decode:
        decode(engine, service, busy)
    with tab_annotate:
        annotate(engine, service, busy)
    with tab_export:
        st.caption('导出已发布素材、关系与原文引文。JSONL 每行一个素材，可作为标注或微调数据的起点；描述为模型或人工整理，使用前需人工抽查。')
        a, b = st.columns(2)
        a.download_button('下载 JSON', service.export('json'), file_name='culture-library.json', mime='application/json')
        b.download_button('下载 JSONL 数据集', service.export('jsonl'), file_name='culture-elements.jsonl', mime='application/jsonl')
