"""Review UI talks only to the semantic service, never to SQLite."""
import math
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
import streamlit as st
import plotly.graph_objects as go
from .profile import DOMAINS as TERMS, ALLOWED, DEFINITIONS, PROPERTIES, uri

STATUS = {'pending': '未发布', 'published': '已发布', 'rejected': '已驳回', 'revoked': '已撤回'}


def batch_name(batch):
    sources = batch.get('sources', [])
    name = batch.get('topic') or (sources[0].get('title') if sources else '') or '未命名资料'
    name = name if len(name) <= 42 else name[:41] + '…'
    try:
        at = datetime.fromisoformat(batch['created_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
    except (ValueError, KeyError):
        at = '时间未知'
    return f"{name} · {at} · {len(sources)}份资料 · {batch['id'][:8]}"


def relation(s):
    code = next((p for p in PROPERTIES if str(uri(p)) == s['predicate']), None)
    # Display wording only; preserve the pinned ontology and stored predicate URI.
    name = '实施者' if code == 'P14' else PROPERTIES[code][1] if code else ''
    return f'{code} {name}' if code else s['predicate']


def label(e):
    return f"{e.get('label', '无效引用')} [{e.get('type', '?')}]" if isinstance(e, dict) else '无效引用'


def title(s):
    return f"{label(s['subject'])} → {relation(s)} → {label(s['object'])}"


def details(s):
    st.text(title(s))
    st.caption(f"{STATUS[s['status']]} · {TERMS[s['domain']]['label']} · 陈述 {s['id']}")
    st.markdown('**模型提供的引文**')
    st.text(s['evidence']['quote'])
    st.caption(f"来源 {s['evidence']['source_id']} · 字符 [{s['evidence']['start']}, {s['evidence']['end']})")
    for warning in s['validation'].get('warnings', []):
        st.warning(warning)
    if s['evidence'].get('match_method') in {'normalized', 'approximate'}:
        st.caption(f"匹配方式：近似定位 · 文字相似度 {s['evidence']['similarity']:.0%}（不是事实置信度）")
        st.text(s['evidence']['matched_quote'])
    if s.get('source'):
        with st.expander('核对来源全文 · ' + s['source'].get('title', '')):
            st.text(s['source']['text'])
    if not s['validation']['valid']:
        st.error('；'.join(s['validation']['errors']))
    verification = s.get('verification')
    if verification:
        st.markdown('**模型证据检查**')
        st.write({'supported': '原文支持', 'unsupported': '原文不支持', 'uncertain': '无法确认'}.get(verification.get('verdict'), '检查失败'))
        st.text(verification.get('reason', ''))
        st.json(verification, expanded=False)
    else:
        st.caption('历史记录未包含模型证据检查。已发布记录保留原有发布依据；未发布候选请重新抽取。')
    with st.expander('版本与发布记录'):
        st.json({'证据': s['evidence'], '实体与类型': [s['subject'], s['object']], '版本': s['versions'],
                 '批次': s['batch_id'], '定位修正历史': s.get('evidence_revisions', []), '审核': s['reviews'], '发布': s['publications']}, expanded=False)


def graph_focus(statements, points):
    """Resolve tagged graph points against the currently filtered published claims."""
    if not points:
        return None
    data = points[-1].get('customdata')
    if not isinstance(data, (list, tuple)):
        return None
    if len(data) == 2 and data[0] == 'node':
        claims = [s for s in statements if any(e['id'] == data[1] for e in (s['subject'], s['object']))]
        if claims:
            entity = next(e for s in claims for e in (s['subject'], s['object']) if e['id'] == data[1])
            return {'node_id': data[1], 'label': entity['label'], 'kind': 'node', 'ids': [s['id'] for s in claims]}
    if len(data) == 4 and data[0] == 'edge':
        claims = [s for s in statements if (s['subject']['id'], s['object']['id'], s['predicate']) == tuple(data[1:])]
        if claims:
            return {'kind': 'edge', 'label': relation(claims[0]), 'ids': [s['id'] for s in claims]}
    return None


def directed_figure(statements, selected_node=None):
    nodes = {e['id']: e for s in statements for e in (s['subject'], s['object'])}
    positions = {key: (math.cos(i * 2 * math.pi / max(1, len(nodes))), math.sin(i * 2 * math.pi / max(1, len(nodes)))) for i, key in enumerate(nodes)}
    fig = go.Figure()
    groups = {}
    for s in statements:
        groups.setdefault((s['subject']['id'], s['object']['id'], s['predicate']), []).append(s)
    pairs = {}
    for source, target, predicate in groups:
        pairs.setdefault((source, target), []).append(predicate)
    for (source, target, predicate), claims in groups.items():
        x, y = positions[source]; tx, ty = positions[target]
        predicates = pairs[(source, target)]
        offset = (predicates.index(predicate) - (len(predicates) - 1) / 2) * .13
        if source == target:
            # Self-relations need a visible loop instead of a zero-length arrow.
            angles = [i * 2 * math.pi / 32 for i in range(33)]
            fig.add_trace(go.Scatter(x=[x + .13 * math.sin(a) for a in angles], y=[y + .13 + .13 * math.cos(a) for a in angles], mode='lines', line={'color': '#168578'}, hoverinfo='skip'))
            fig.add_annotation(x=x + .1, y=y + .05, ax=x + .13, ay=y + .13, axref='x', ayref='y', xref='x', yref='y', showarrow=True, arrowhead=3)
            mx, my = x + offset, y + .32
        else:
            fig.add_annotation(x=tx * .9 + x * .1, y=ty * .9 + y * .1, ax=x, ay=y, axref='x', ayref='y',
                               xref='x', yref='y', showarrow=True, arrowhead=3, arrowsize=1.5, arrowwidth=1.5, arrowcolor='#168578')
            mx, my = (x + tx) / 2, (y + ty) / 2 + (.06 if source < target else -.06) + offset
        fig.add_trace(go.Scatter(x=[mx], y=[my], mode='markers+text',
                                marker={'size': 11, 'symbol': 'diamond', 'color': '#95bdb2'}, textposition='top center',
                                customdata=[['edge', source, target, predicate]],
                                text=[escape(relation(claims[0]))],
                                hovertext=['<br>'.join(escape(title(s)) for s in claims)],
                                hovertemplate='%{hovertext}<extra>点击查看关系与原文</extra>', textfont={'size': 12, 'color': '#264e55'}))
    fig.add_trace(go.Scatter(x=[positions[k][0] for k in nodes], y=[positions[k][1] for k in nodes], mode='markers+text',
                            text=[escape(e['label']) for e in nodes.values()], textposition='top center',
                            marker={'size': [21 if k == selected_node else 16 for k in nodes],
                                    'color': ['#bc9654' if k == selected_node else '#168578' for k in nodes]},
                            customdata=[['node', k] for k in nodes], hovertext=[escape(label(e)) for e in nodes.values()],
                            hovertemplate='%{hovertext}<extra>点击查看关联陈述</extra>'))
    fig.update_layout(showlegend=False, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                      font={'family': 'PingFang SC, Microsoft YaHei, sans-serif', 'color': '#34564a'},
                      hoverlabel={'bgcolor': '#ffffff', 'font': {'color': '#264e55', 'size': 13}, 'align': 'left'}, height=520,
                      clickmode='event+select', dragmode='pan',
                      xaxis={'visible': False, 'range': [-1.4, 1.4]}, yaxis={'visible': False, 'range': [-1.4, 1.4]}, margin={'l': 10, 'r': 10, 't': 25, 'b': 10})
    return fig


def semantic_page(engine):
    from lab.workbench_ui import job_panel, header, section_heading
    service = engine.workbench.semantic
    header('探索有据可查的知识', '选择一批资料，沿着人物、活动和器物之间的关系，回到知识的原始出处。', 'ATLAS / SEMANTIC COLLECTION')
    with st.expander('最近构建任务', expanded=bool(engine.workbench.active)):
        job_panel(engine)
    with st.container(key='batch-panel'):
        section_heading('COLLECTION FILTERS', '选择你的知识集合')
        a, b, c = st.columns(3)
        domain = a.selectbox('领域', [''] + list(TERMS), format_func=lambda k: TERMS[k]['label'] if k else '全部领域')
        kind = b.selectbox('实体类型', [''] + ALLOWED, format_func=lambda k: f'{k} {DEFINITIONS[k][1]}' if k else '全部类型')
        sources = engine.workbench.sources.list()
        names = {s['id']: s['title'] for s in sources}
        source = c.selectbox('来源', [''] + list(names), format_func=lambda k: names[k] if k else '全部来源')
        batches = service.batches()
        batch_map = {b['id']: b for b in batches}
        batch_ids = [''] + [b['id'] for b in batches]
        selected_batch = st.query_params.get('batch', '')
        batch = st.selectbox('抽取批次', batch_ids, index=batch_ids.index(selected_batch) if selected_batch in batch_ids else 0,
                             format_func=lambda k: batch_name(batch_map[k]) if k else '全部批次')
        st.caption('批次时间为北京时间；自动构建显示研究主题，手动构建显示首份资料标题。')
        if batch:
            selected = batch_map[batch]
            st.write('**本批次资料：** ' + '、'.join(s.get('title') or '未命名资料' for s in selected['sources']))
            st.caption(f"完整批次 ID：{batch} · 模型：{selected.get('model', {}).get('model', '未知')} · 状态：{selected['status']}")
    values = service.list_candidates(domain=domain, entity_type=kind, source_id=source, batch_id=batch)
    published_values = [s for s in values if s['status'] == 'published']
    node_count = len({e['id'] for s in published_values for e in (s['subject'], s['object'])})
    with st.container(key='summary-metrics'):
        columns = st.columns(4)
        for col, label_, count in zip(columns, ['已发布关系', '关联实体', '未发布陈述', '抽取批次'], [len(published_values), node_count, sum(s['status'] == 'pending' for s in values), len(batches)]):
            col.metric(label_, count)
    graph, results, reports = st.tabs(['已发布图谱', '抽取结果', '校验报告'],
                                     key='semantic-tabs', on_change='rerun')
    if graph.open:
        with graph:
            published = [s for s in values if s['status'] == 'published']
            if published:
                scope = hashlib.sha256('|'.join(s['id'] for s in published).encode()).hexdigest()[:16]
                chart_key, focus_key, claim_key = 'semantic-map-' + scope, 'semantic-focus-' + scope, 'semantic-claim-' + scope
                focus = st.session_state.get(focus_key)
                choices = [s for s in published if not focus or s['id'] in focus['ids']]
                choice_map = {s['id']: s for s in choices}
                if st.session_state.get(claim_key) not in choice_map:
                    st.session_state[claim_key] = choices[0]['id']

                def select_graph_point():
                    event = st.session_state.get(chart_key, {})
                    selected = graph_focus(published, event.get('selection', {}).get('points', []))
                    st.session_state[focus_key] = selected
                    ids = selected['ids'] if selected else [s['id'] for s in published]
                    st.session_state[claim_key] = ids[0]

                canvas, inspector = st.columns([1.65, 1], gap='large')
                with canvas, st.container(key='graph-canvas'):
                    section_heading('KNOWLEDGE MAP', '关系图谱', '点击节点查看关联陈述，点击关系旁的菱形查看对应原文。')
                    st.plotly_chart(directed_figure(published[:80], (focus or {}).get('node_id')), key=chart_key,
                                    on_select=select_graph_point, selection_mode='points', width='stretch', theme=None)
                    st.caption(f'显示 {min(80, len(published))} / {len(published)} 条关系；完整数据保留在明细和导出中。')
                with inspector, st.container(key='claim-inspector'):
                    section_heading('EVIDENCE INSPECTOR', '关系与原文')
                    if focus:
                        st.caption(f"已选择{'节点' if focus['kind'] == 'node' else '关系'}：{focus['label']} · {len(choices)} 条关联陈述")
                        if st.button('查看全部关系'):
                            st.session_state[focus_key] = None
                            st.rerun()
                    picked_id = st.selectbox('查看已发布陈述', list(choice_map), key=claim_key,
                                             format_func=lambda cid: title(choice_map[cid]))
                    picked = choice_map[picked_id]
                    details(picked)
                    if st.button('撤回当前陈述'):
                        service.revoke([picked['id']]); st.rerun()
            else:
                st.info('尚无符合筛选条件的已发布知识。本体校验与模型证据检查通过后将自动发布。')
            st.caption('导出完整正式图谱，不受页面筛选影响。审计包另含已撤回记录；不包含校验失败或尚未发布的内容。')
            export_columns = st.columns(3)
            for column, (fmt, filename, mime) in zip(export_columns, [('turtle', 'culture-graph.ttl', 'text/turtle'), ('json-ld', 'culture-graph.jsonld', 'application/ld+json'), ('json', 'culture-audit.json', 'application/json')]):
                column.download_button('导出 ' + fmt, service.export(fmt), file_name=filename, mime=mime, width='stretch')
            history = [s for s in values if s['status'] in {'rejected', 'revoked'}]
            if history:
                with st.expander('驳回与撤回历史'):
                    picked = st.selectbox('查看历史陈述', history, format_func=title)
                    details(picked)
    if results.open:
        with results:
            if not values:
                st.info('请在资料工作台选择资料并构建标准语义图谱。')
            else:
                st.caption(f'共 {len(values)} 条结果，选择一条查看原文和证据，避免一次加载全部正文。')
                picked = st.selectbox('查看抽取陈述', values, format_func=lambda s: STATUS[s['status']] + ' · ' + title(s))
                details(picked)
    if reports.open:
        with reports:
            if st.button('重新校验并自动发布'):
                service.auto_publish(); st.rerun()
            report_batches = [batch_map[batch]] if batch else batches
            for report_batch in report_batches:
                with st.expander(batch_name(report_batch) + ' · ' + report_batch['status']):
                    st.json({k: v for k, v in report_batch.items() if k != 'sources'}, expanded=False)
                    blocks = service.report(report_batch['id'])
                    for block in blocks:
                        st.caption(f"文本块 {block['id'][:8]} · {block['status']} · 字符 [{block['start']}, {block['end']})")
                        if block['error']:
                            st.error(block['error'])
                        if block['deferred']:
                            st.write('待建模：', block['deferred'])
                    if report_batch['status'] in {'partial', 'failed', 'interrupted'}:
                        st.caption(f"重试模型：{engine.workbench.llm.settings.model}。失败或中断块的正文将再次发送至已配置服务，成功块不会重复调用。")
                        if st.button('重试失败或中断块', key='semantic-retry-' + report_batch['id'], disabled=bool(engine.workbench.active)):
                            try:
                                service.retry(report_batch['id']); st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))
            invalid = [s for s in values if not s['validation']['valid']]
            if invalid:
                picked = st.selectbox('查看未通过校验的陈述', invalid, format_func=title)
                details(picked)
