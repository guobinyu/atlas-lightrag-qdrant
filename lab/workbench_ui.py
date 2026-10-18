from html import escape
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from lab.charts import scatter
from lab.graph import graph_figure, subgraph
from lab.llm import LLMSettings
from lab.sources import MAX_BYTES, parse_document, search_web

PAGES = {"collect": "资料工作台", "demand": "产品需求探索", "campaign": "热点营销", "graph": "知识图谱", "semantic": "标准语义图谱", "vectors": "向量数据库", "retrieve": "检索观察台", "settings": "模型设置"}
STATUS = {"collected": "待构建", "pending": "等待处理", "indexing": "构建中", "processing": "处理中", "processed": "已入库", "failed": "失败", "interrupted": "中断", "preprocessed": "待完成"}
EVENTS = {"job.started": "任务开始", "source.fetching": "读取网页正文", "source.collected": "资料已保存", "source.failed": "资料读取失败", "document.started": "文档切块与抽取", "llm.started": "调用抽取模型", "llm.completed": "模型返回", "llm.failed": "模型调用失败", "document.completed": "图谱与向量已写入", "document.failed": "文档构建失败", "indexes.refreshing": "读取最新索引", "build.completed": "构建完成", "collection.completed": "采集完成", "job.failed": "任务失败"}
EVENTS.update({"embedding.started": "向量化开始", "embedding.completed": "向量化完成", "vectors.written": "Qdrant 写入完成"})
EVENTS.update({"semantic.chunk.started": "语义文本块开始抽取", "semantic.chunk.completed": "语义候选已保存", "semantic.chunk.failed": "语义文本块失败"})
EVENTS.update({"semantic.verification.started": "模型证据检查", "semantic.verification.completed": "证据检查完成", "semantic.batch.created": "语义批次已创建"})
EVENTS.update({"retry.started": "重试工作区未完成文档", "retry.completed": "重试完成"})
EVENTS['campaign.progress'] = '热点营销进度'
EVENTS['demand.progress'] = '产品需求探索进度'


def navigation():
    route = st.query_params.get("view", "collect")
    if route not in PAGES:
        route = "collect"
    if st.session_state.get("navigation_url") != route:
        st.session_state.workspace_page = route
        st.session_state.navigation_url = route
    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-icon">◈</div><div>Atlas<small>KNOWLEDGE WORKSPACE</small></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="side-label">工作区导航</div>', unsafe_allow_html=True)
        choice = st.radio("工作区导航", list(PAGES), index=None, format_func=PAGES.get, key="workspace_page", label_visibility="collapsed", width="stretch")
        if choice != route:
            st.query_params["view"] = choice
            st.session_state.navigation_url = choice
        st.divider()
    return choice


def header(title, subtitle, tag="ATLAS / KNOWLEDGE WORKSPACE"):
    st.markdown(f'<div class="page-header"><div class="eyebrow">{escape(tag)}<span>资料 · 关系 · 证据</span></div><h1>{escape(title)}</h1><p class="subtitle">{escape(subtitle)}</p></div>', unsafe_allow_html=True)


def section_heading(kicker, title, copy=''):
    st.markdown(f'<div class="section-eyebrow">{escape(kicker)}</div><div class="section-title">{escape(title)}</div><div class="section-copy">{escape(copy)}</div>', unsafe_allow_html=True)


def show_links():
    a, b, c = st.columns(3)
    a.link_button("打开最新知识图谱", "/?view=graph", width="stretch")
    b.link_button("打开向量数据库", "/?view=vectors", width="stretch")
    c.link_button("观察相似度检索", "/?view=retrieve", width="stretch")


@st.fragment(run_every=1)
def job_panel(engine):
    work = engine.workbench
    jobs = work.jobs()
    if not jobs:
        return
    job = work.active.snapshot() if work.active else jobs[0]
    status = job["status"]
    previous = st.session_state.get("last_job_status")
    busy = work.active is not None
    st.session_state.last_job_status = (job["id"], busy)
    if previous == (job["id"], True) and not busy:
        # Rebuild source checkboxes and action states once when the worker ends.
        st.rerun()
    with st.container(key='task-panel'):
        status_label = {"running": "正在执行", "success": "已完成", "partial": "部分完成", "failed": "执行失败", "interrupted": "进程中断"}.get(status, status)
        st.markdown(f'<div class="task-top"><strong>{"当前执行" if status == "running" else "最近执行"}</strong><span class="task-badge {escape(status)}">{escape(status_label)}</span></div>', unsafe_allow_html=True)
        a, b = st.columns([3, 1])
        created = datetime.fromisoformat(job['created_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
        a.caption(f"{('产品需求探索' if job['kind'].startswith('demand_') else '热点营销' if job['kind'].startswith('campaign_') else '本体图谱构建' if job['kind'].startswith('semantic') else '资料采集' if job['kind'] == 'collect' else 'LightRAG 构建')} · {created} 北京时间 · #{job['id'][:8]}")
        if job['kind'].startswith('demand_'):
            st.write('当前阶段：' + job.get('phase', '准备任务'))
            st.link_button('查看产品需求', '/?view=demand&demand=' + job['demand_id'])
        if job['kind'].startswith('campaign_'):
            st.caption(' → '.join(['理解需求', '搜集资料', '推理品牌与产品', '品牌匹配', '生成方案']))
            st.write('当前阶段：' + job.get('phase', '理解需求'))
            counts = job.get('counts', {})
            st.caption(f"已读取 {counts.get('sources', 0)} 份正文 · 已提取 {counts.get('extracted', 0)} 份 · 已匹配 {counts.get('matched', 0)} 个话题")
            st.link_button('查看热点营销结果', '/?view=campaign&campaign=' + job['campaign_id'])
        b.caption(job.get('topic', ''))
        if job['kind'].startswith('semantic'):
            phase = 1 if job.get('batch_id') else 0
            if status != 'running':
                phase = 2
            stages = []
            for index, label in enumerate(['搜集资料', '抽取与校验', '查看图谱']):
                state = 'done' if index < phase else 'active' if status == 'running' and index == phase else ''
                if index == 2 and job.get('semantic_result', {}).get('published', 0):
                    state = 'done'
                if status in {'failed', 'interrupted'}:
                    state = ''
                stages.append(f'<div class="task-stage {state}">{index + 1:02d}　{label}</div>')
            st.markdown('<div class="task-stages">' + ''.join(stages) + '</div>', unsafe_allow_html=True)
        if status == "running":
            st.info("任务在后台执行，可以切换页面。请勿关闭服务进程；同一工作区同时只执行一个任务。")
        if job.get("message"):
            if job['kind'].startswith('semantic') and job.get('publication_policy') != 'automatic-evidence-v2':
                st.caption('以下为历史任务在当时版本保存的说明；当前新任务采用模型证据检查后自动发布。')
            if status == 'success':
                st.caption(job['message'])
            else:
                st.warning(job["message"])
        if job.get('semantic_result'):
            result = job['semantic_result']
            if result.get('requested') is not None:
                st.write(f"目标 {result['requested']} 份 · 已读取 {result['collected']} 份")
                if result['collected'] < result['requested']:
                    st.warning('可读取资料不足目标数量，未用重复资料补足。可调整主题重试。')
            metrics = st.columns(2)
            for index, (name, value) in enumerate(zip(['抽取陈述', '已发布', '未发布', '失败文本块'], [result['extracted'], result['published'], result['blocked'], result['failed_blocks']])):
                metrics[index % 2].metric(name, value)
            if result.get('batch_id'):
                st.link_button('查看本次标准语义图谱', '/?view=semantic&batch=' + result['batch_id'])
            if result.get('failures'):
                with st.expander('搜索和正文读取失败记录'):
                    st.json(result['failures'])
        if job.get("result"):
            result = job["result"]
            st.success(f"当前知识库：{result['entities']} 个实体 · {result['relations']} 条关系 · {sum(result['vectors'].values())} 个向量")
            if result["new_entities"] == 0 and result["new_relations"] == 0:
                st.caption("本次没有新增实体或关系；可能为重复资料，或模型未抽取到有效内容。")
            show_links()
        events = job["events"]
        with st.expander(f"全部执行记录 · {len(events)} 个事件", expanded=status == "running"):
            for event in events[-4:]:
                data = event["data"]
                suffix = data.get("title") or data.get("model") or data.get("message") or ""
                elapsed = f" · {data['duration_ms'] / 1000:.1f}s" if "duration_ms" in data else ""
                st.caption(f"{event['at'][11:19]}　{EVENTS.get(event['event_type'],event['event_type'])}　{suffix}{elapsed}")
            st.json(events, expanded=False)
            st.download_button("导出任务记录", json.dumps(job, ensure_ascii=False, indent=2), file_name=f"build-{job['id'][:8]}.json", mime="application/json", key="export-current-job")
        if status != "running" and st.button("刷新资料和图谱状态", key="refresh-after-job", width="stretch"):
            try:
                if not job["kind"].startswith("semantic"):
                    work.refresh().result(timeout=60)
                st.rerun()
            except Exception:
                st.error("刷新失败，请等待其他任务完成后再试。")


def start_job(engine, kind, inputs):
    try:
        future, job = engine.workbench.start(kind, inputs)
        # Keep ownership in the engine; widget reruns never resubmit this future.
        st.session_state["last_job_id"] = job.value["id"]
        st.rerun()
    except Exception as exc:
        st.error(str(exc))


def collect_page(engine):
    work = engine.workbench
    header("让文化知识，连成图谱", "从一个研究主题出发，搜集资料、提取有据可查的知识，在图谱中探索它们的联系。")
    steps = [('01', '确定主题', '选一个想了解的文化方向'), ('02', '搜集资料', '读取网页，保留原始来源'), ('03', '抽取与校验', '按本体建模，检查原文依据'), ('04', '探索图谱', '自动发布，查看关系与证据')]
    st.markdown('<div class="workspace-flow">' + ''.join(f'<div class="workspace-step"><b>{n}</b><div><strong>{name}</strong><small>{note}</small></div></div>' for n, name, note in steps) + '</div>', unsafe_allow_html=True)
    from lab.ontology.profile import DOMAINS
    intake, activity = st.columns([1.5, 1], gap='medium')
    with intake:
        with st.container(key='auto-build-panel'):
            section_heading('START A COLLECTION', '你想探索什么？', '输入主题，其余步骤交给工作台。建议从 3 份资料开始。')
            with st.form('semantic-auto', border=False):
                auto_topic = st.text_input('研究主题', '宋朝文化', max_chars=300, key='auto-topic')
                a, b = st.columns(2)
                auto_limit = a.number_input('希望读取的资料数量', 1, 10, 3)
                auto_domain = b.selectbox('本体领域', ['culture', 'silk', 'tea', 'wine'], format_func=lambda k: DOMAINS[k]['label'])
                automatic = st.form_submit_button('一键搜集并构建本体图谱', type='primary', width='stretch', disabled=bool(work.active) or not work.llm.configured)
            st.markdown(f'<div class="connection-pill {"" if work.llm.configured else "offline"}"><i></i>{escape(work.llm.settings.model or "请先连接模型")} · CIDOC CRM 7.1.3</div>', unsafe_allow_html=True)
            st.caption('正文将发送至已配置模型。本体校验与证据检查通过后自动发布，无需人工审核。')
            with st.expander('了解资料与抽取范围'):
                st.caption('最多 10 份资料，每份使用前 9,000 字符。资料相关性主要由检索词决定，尚无独立相关性评分。')
            if automatic:
                start_job(engine, 'semantic_auto', {'topic': auto_topic, 'limit': int(auto_limit), 'domain': auto_domain})
    with activity, st.container(key='collection-activity'):
        if work.jobs():
            job_panel(engine)
        else:
            section_heading('YOUR WORKSPACE', '从第一份资料开始', '输入主题后，任务进度和处理结果会显示在这里。')
            st.markdown('**1. 搜集资料**　保留正文和来源\n\n**2. 构建知识**　提取实体与关系\n\n**3. 探索结果**　查看图谱或开始营销策划')
        st.link_button('打开热点营销 →', '/?view=campaign', width='stretch')
    if not work.llm.configured:
        st.info("资料搜索和预览已可用。生成图谱前，请在「模型设置」连接用于实体关系抽取的 LLM；当前向量模型已经可用。")
    with st.expander("手动搜集资料 · 搜索 / 网页链接 / 上传", expanded=False):
        tab_search, tab_url, tab_upload = st.tabs(["搜索公开资料", "添加网页链接", "上传 / 粘贴资料"])
        with tab_search:
            with st.form("research-search"):
                topic = st.text_input("研究主题", placeholder="例如：缂丝工艺、传统织锦与丝绸之路文化交流", value="丝绸文化 缂丝 云锦 传统织造技艺")
                left, right = st.columns([3, 1], vertical_alignment="bottom")
                limit = left.select_slider("候选资料数量", options=[3, 6, 10], value=6)
                search = right.form_submit_button("查找资料", type="primary", width="stretch")
            st.caption("通过公开搜索引擎查找候选链接；摘要用于筛选，只有实际读取的正文才会入库。")
            if search:
                try:
                    with st.spinner("正在查找公开资料…"):
                        results = search_web(topic, limit)
                    st.session_state.search_results = results
                    st.session_state.search_topic = topic
                    st.session_state.search_revision = st.session_state.get("search_revision", 0) + 1
                except Exception as exc:
                    st.error(str(exc))
            results = st.session_state.get("search_results", [])
            if results:
                st.caption(f"关于「{st.session_state.get('search_topic','')}」找到 {len(results)} 个候选")
                chosen = []
                for i, result in enumerate(results):
                    with st.container(border=True):
                        if st.checkbox(result["title"], key=f"candidate-{st.session_state.get('search_revision',0)}-{i}"):
                            chosen.append(result)
                        st.caption(result["snippet"][:420])
                        st.link_button("查看原网页 ↗", result["url"])
                if st.button(f"读取选中资料的正文（{len(chosen)}）", type="primary", disabled=not chosen or bool(work.active)):
                    start_job(engine, "collect", chosen)
            elif "search_results" in st.session_state:
                st.info("本次没有搜索结果，可尝试更短的关键词，或直接添加网页链接。")
        with tab_url:
            with st.form("url-source"):
                urls = st.text_area("公开网页 URL，每行一个，最多 10 个", placeholder="粘贴丝绸博物馆、非遗机构等发布的公开文章链接")
                add_urls = st.form_submit_button("读取网页正文", type="primary", disabled=bool(work.active))
            if add_urls:
                values = list(dict.fromkeys(u.strip() for u in urls.splitlines() if u.strip()))
                start_job(engine, "collect", [{"url": u, "title": u} for u in values])
        with tab_upload:
            uploads = st.file_uploader("支持 TXT、Markdown、文本型 PDF · 每份最多 4 MB", type=["txt", "md", "pdf"], accept_multiple_files=True)
            if st.button("保存上传资料", disabled=not uploads or bool(work.active)):
                for upload in uploads[:10]:
                    try:
                        data = parse_document(upload.name, upload.getvalue())
                        work.sources.add({**data, "title": upload.name, "url": "", "filename": upload.name})
                    except Exception as exc:
                        st.error(f"{upload.name}：{exc}")
                st.success("有效资料已保存到下方资料库，可预览后构建。")
            with st.expander("直接粘贴文本"):
                with st.form("paste-source"):
                    title = st.text_input("资料标题")
                    text = st.text_area("正文", height=160, max_chars=60000)
                    save_text = st.form_submit_button("保存正文", disabled=bool(work.active))
                if save_text:
                    try:
                        work.sources.add({**parse_document("pasted.txt", text.encode()), "title": title.strip() or "粘贴资料", "url": ""})
                        st.success("已保存。请在下方勾选并构建。")
                    except Exception as exc:
                        st.error(str(exc))
    st.markdown('<div class="section-space"></div>', unsafe_allow_html=True)
    section_heading("YOUR SOURCES", "资料库", "已保存的正文与来源，可选择后再次构建。")
    sources = work.sources.list()
    if not sources:
        st.info("先搜索、添加链接或上传一份资料。已入库的文本向量可在检索观察台查看。")
        return
    selected = []
    st.caption(f"共 {len(sources)} 份资料 · 勾选正文后，在下方选择构建方式。")
    with st.container(height=480 if len(sources) > 3 else "content", border=False, key="source-library"):
        for source in sources:
            with st.container(border=True):
                row, detail = st.columns([4, 1])
                with row:
                    if st.checkbox(f"{source['title']} · {source['chars']:,} 字符 · {STATUS.get(source['status'],source['status'])}", key=f"source-{source['id']}"):
                        selected.append(source["id"])
                with detail:
                    if source.get("url"):
                        st.link_button("来源 ↗", source["url"])
                with st.expander("预览正文 · " + source["title"][:65]):
                    if source.get("truncated"):
                        st.warning(f"原正文 {source['original_chars']:,} 字符，当前保存 {source['chars']:,} 字符；构建使用的内容如下。")
                    with st.container(height=230):
                        st.text(source["text"])
                    st.caption(f"资料 ID {source['id']} · LightRAG {source['doc_id']}")
    st.caption("构建会将所选正文发送至模型设置中的抽取服务，生成实体、关系与三类向量。使用本机 Ollama 时抽取在本机执行。")
    if st.button(f"用 LightRAG 构建所选资料（{len(selected)}）", type="primary", width="stretch", disabled=not selected or bool(work.active) or not work.llm.configured):
        start_job(engine, "build", selected)
    domain = st.selectbox("标准语义图谱领域", ['culture', 'silk', 'tea', 'wine'], format_func=lambda k: DOMAINS[k]["label"])
    st.caption(f"语义抽取模型：{work.llm.settings.model or '未配置'}。正文将发送到已配置服务；本体校验和模型证据检查均通过后自动发布，无需人工审核。")
    if st.button(f"构建标准语义图谱（{len(selected)}）", width="stretch", disabled=not selected or bool(work.active) or not work.llm.configured):
        start_job(engine, "semantic", {"source_ids": selected, "domain": domain})
    st.link_button("查看标准语义图谱", "/?view=semantic")
    if any(s["status"] in {"failed", "interrupted", "processing", "indexing", "pending"} for s in sources):
        with st.expander("失败与中断恢复"):
            st.caption("LightRAG 的原生重试作用于当前工作区的所有失败、等待和中断文档，每份失败文档重试一次。请先修复模型设置，再执行。")
            if st.button("重试工作区未完成文档", disabled=bool(work.active) or not work.llm.configured):
                start_job(engine, "retry", [])


def snapshot(engine):
    work = engine.workbench
    if work.active:
        st.info("构建/采集中；当前显示最近完成的索引快照，完成后可刷新。")
        if work.cached_snapshot is None:
            return None
    elif work.cached_snapshot is None:
        with st.spinner("读取 LightRAG 与 Qdrant 的真实索引…"):
            work.refresh().result(timeout=90)
    if st.button("刷新索引视图", disabled=bool(work.active)):
        work.refresh().result(timeout=90)
    return work.cached_snapshot


def evidence(item, engine):
    st.caption("来源文件 / 文本块")
    file_paths = str(item.get("file_path", "")).split("<SEP>")
    sources = engine.workbench.sources.list()
    matches = [s for s in sources if s["file_path"] in file_paths]
    for source in matches:
        st.markdown("**" + escape(source["title"]) + "**")
        if source.get("url"):
            st.link_button("打开原始来源", source["url"])
        with st.expander("查看采集正文 · " + source["id"][:6]):
            st.text(source["text"])
    st.code(item.get("source_id", item.get("id", "")), language=None, wrap_lines=True)
    if not matches:
        st.caption(item.get("file_path", "未提供文件来源"))


def graph_page(engine):
    header("让知识之间的联系可见", "探索 LightRAG 实际抽取的实体和关系，点选节点或关系中点查看描述与原始资料。", "ATLAS / KNOWLEDGE GRAPH")
    data = snapshot(engine)
    if data is None:
        return
    nodes, edges = data["nodes"], data["edges"]
    with st.container(key='summary-metrics'):
        a, b, c = st.columns(3)
        a.metric("实体", len(nodes))
        b.metric("关系", len(edges))
        c.metric("Qdrant 向量", sum(data["counts"].values()))
    if not nodes:
        st.info("当前知识库尚未生成实体图谱。请在资料工作台勾选正文并执行 LightRAG 构建。原有文本向量不会自动变成图谱。")
        st.link_button("去收集与构建资料", "/?view=collect")
        return
    a, b, c = st.columns([3, 2, 1])
    focus = a.selectbox("搜索实体并展开邻居", [""] + sorted(n["id"] for n in nodes), format_func=lambda n: n or "全部实体（按连接数展示）")
    kind = b.selectbox("实体类型", ["全部"] + sorted({n.get("entity_type", "未知") for n in nodes}))
    depth = c.selectbox("邻居层数", [1, 2, 3])
    shown_nodes, shown_edges = subgraph(nodes, edges, focus, depth, kind)
    st.caption(f"当前显示 {len(shown_nodes)} / {len(nodes)} 个实体，{len(shown_edges)} / {len(edges)} 条关系 · 最多展示 160 个实体 · 图谱模型抽取结果需结合原文核查")
    left, right = st.columns([2.2, 1])
    with left, st.container(key="knowledge-canvas"):
        section_heading("KNOWLEDGE MAP", "实体关系图")
        chart_key = f"knowledge-graph-{focus}-{kind}-{depth}-{data['updated_at']}"
        selection = st.plotly_chart(graph_figure(shown_nodes, shown_edges), key=chart_key, on_select="rerun", selection_mode="points", width="stretch", theme=None, config={"displaylogo": False})
    with right, st.container(key="knowledge-inspector"):
        section_heading("INSPECTOR", "知识检查器")
        options = [n["id"] for n in shown_nodes]
        picked = selection.selection.points
        selected = None
        if picked and picked[-1].get("customdata"):
            tag, value = picked[-1]["customdata"]
            if tag == "node":
                selected = next((n for n in shown_nodes if n["id"] == value), None)
            elif tag == "edge" and int(value) < len(shown_edges):
                selected = shown_edges[int(value)]
        if selected is None and options:
            name = st.selectbox("选择实体", options)
            selected = next(n for n in shown_nodes if n["id"] == name)
        if selected:
            st.markdown("**" + escape(selected.get("id", f"{selected.get('source')} ↔ {selected.get('target')}")) + "**")
            st.caption(selected.get("entity_type", selected.get("keywords", "关系")))
            st.text(selected.get("description", "暂无描述"))
            evidence(selected, engine)
    with st.expander("关系明细"):
        st.dataframe([{k: e.get(k) for k in ("source", "target", "keywords", "description", "weight")} for e in shown_edges], hide_index=True, width="stretch")
    st.download_button("导出完整图谱 JSON", json.dumps({"nodes": nodes, "edges": edges, "updated_at": data["updated_at"]}, ensure_ascii=False, indent=2), file_name="knowledge-graph.json", mime="application/json")


def vector_page(engine):
    header("探索知识的向量空间", "直接读取 Qdrant 中的文本、实体与关系向量。每一类空间独立投影，并保留真实 payload。", "ATLAS / VECTOR EXPLORER")
    data = snapshot(engine)
    if data is None:
        return
    names = {"chunks": "文本块", "entities": "实体", "relationships": "关系"}
    with st.container(key='summary-metrics'):
        columns = st.columns(3)
        for col, name in zip(columns, names):
            col.metric(names[name] + "向量", data["counts"][name])
    kind = st.segmented_control("向量集合", list(names), default="chunks", format_func=names.get, selection_mode="single") or "chunks"
    space = data["spaces"][kind]
    st.caption(f"{space['collection']} · {engine.settings.embedding_dim} 维 · Cosine · workspace={engine.settings.workspace}")
    plot = space["plot"]
    if not plot["points"]:
        st.info("这个集合还没有向量。完成 LightRAG 构建后会写入相应实体与关系向量。")
        return
    left, right = st.columns([2, 1])
    with left, st.container(key="vector-canvas"):
        section_heading("VECTOR SPACE", "向量分布")
        result = st.plotly_chart(scatter(plot, [], show_query=False, show_hits=False, exploration=True), key="vectors-" + kind, on_select="rerun", selection_mode="points", width="stretch", theme=None)
        st.caption(f"展示 {len(plot['points'])} / {space['total']} 个真实向量 · PCA 保留方差 {plot['variance']:.1%} · 各集合坐标不能直接比较")
        if plot.get("note"):
            st.caption(plot["note"])
    with right, st.container(key="vector-inspector"):
        section_heading("INSPECTOR", "向量记录")
        selected = None
        if result.selection.points:
            custom = result.selection.points[-1].get("customdata", [])
            if custom:
                selected = next((p for p in plot["points"] if p["point_id"] == custom[0]), None)
        if selected is None:
            index = st.selectbox("查看向量", range(len(plot["points"])), format_func=lambda i: plot["points"][i]["content"][:45].replace("\n", " "), key="inspect-" + kind)
            selected = plot["points"][index]
        st.caption("Qdrant point ID")
        st.code(selected["point_id"], language=None, wrap_lines=True)
        st.text(selected["content"])
        with st.expander("实际 payload"):
            st.json(selected["payload"])
        evidence(selected["payload"], engine)
    if engine.settings.qdrant_mode == "server":
        st.link_button("打开 Qdrant 官方 Dashboard", engine.settings.qdrant_url.rstrip("/") + "/dashboard")
    else:
        st.caption("当前为 Qdrant 本地持久化引擎；这里展示真实数据库记录。独立 Qdrant Server 的 Dashboard 可在配置连接后打开。")
    st.link_button("用问题观察相似度检索过程 →", "/?view=retrieve")


def settings_page(engine):
    work = engine.workbench
    header("模型与服务设置", "文字模型负责需求分析与设计方案，图片模型负责生成与修改图稿。两类服务独立配置。", "ATLAS / MODEL CONNECTIONS")
    text_tab, image_tab = st.tabs(["文字模型", "图片模型"])
    with text_tab:
        text_settings_panel(engine)
    with image_tab:
        image_settings_panel(work)


def text_settings_panel(engine):
    work = engine.workbench
    current = work.llm.settings
    connection, help_panel = st.columns([1.65, 1], gap='medium')
    with connection, st.container(key='settings-connection'):
        section_heading('MODEL CONNECTION', '文字模型连接', '用于需求分析、设计方案与知识抽取。')
        with st.form("extraction-model"):
            provider = st.selectbox("模型服务", ["ollama", "http"], index=0 if current.provider == "ollama" else 1, format_func=lambda p: "本机 Ollama" if p == "ollama" else "Chat Completions 兼容 API")
            endpoint = st.text_input("完整接口地址", current.endpoint, help="Ollama 使用 /api/chat；兼容 API 通常使用 /v1/chat/completions。")
            model = st.text_input("模型名称", current.model, placeholder="例如：qwen3:4b 或服务提供的模型名")
            key = st.text_input("API Key（本机 Ollama 可留空；留空保留已保存的 Key）", type="password", value="")
            clear_key = st.checkbox("清除已保存的 API Key", value=False)
            timeout = st.number_input("单次模型调用超时（秒）", 15, 600, current.timeout, step=15)
            save = st.form_submit_button("测试连接并保存", type="primary", disabled=bool(work.active))
        if save:
            retained_key = current.api_key if endpoint.strip() == current.endpoint and provider == current.provider else ""
            candidate = LLMSettings(provider, endpoint.strip(), model.strip(), "" if clear_key else key or retained_key, int(timeout))
            try:
                with st.spinner("正在向指定服务发送简短测试请求…"):
                    reply = work.configure_llm(candidate).result(timeout=candidate.timeout + 10)
                st.success("连接成功，已保存。现在可以返回工作台继续分析与设计。")
                st.caption(f"模型：{reply['model']} · 回复：{reply['reply']}")
            except Exception as exc:
                st.error(str(exc).replace(candidate.api_key, "[redacted]") if candidate.api_key else str(exc))
    with help_panel, st.container(key='settings-help'):
        section_heading('CONNECTION STATUS', '当前环境')
        if work.llm.configured:
            st.success('抽取模型已配置')
        else:
            st.info('请先填写模型连接')
        st.markdown('**向量模型**')
        st.write(engine.settings.embedding_model)
        st.caption(f'{engine.settings.embedding_dim} 维 · 已就绪')
        st.divider()
        st.caption("配置只保存在本机权限为 600 的文件中；API Key 不进入任务记录。连接测试仅证明服务可用，实际抽取质量取决于模型。")
        with st.expander("使用本机 Ollama"):
            st.markdown("安装并启动 Ollama，下载能处理中英文和结构化抽取的模型，例如 `ollama pull qwen3:4b`。然后填写模型名及 `http://127.0.0.1:11434/api/chat`。小模型适合体验，正式构建应使用经过评估的模型。")
        with st.expander("当前存储配置"):
            st.json({"workspace": engine.settings.workspace, "qdrant_mode": engine.settings.qdrant_mode, "qdrant_url": engine.settings.qdrant_url if engine.settings.qdrant_mode == "server" else "local files", "graph_storage": "LightRAG NetworkX / GraphML", "embedding": engine.settings.embedding_identity()})
            st.caption("向量模型和已有向量必须保持一致。更改 Qdrant / Embedding 请按 README 修改 .env 并重启服务。")


@st.fragment(run_every=2)
def image_test_status(work):
    jobs = [j for j in work.jobs() if j['kind'] == 'image_test']
    if not jobs:
        return
    latest = jobs[0]
    state_key = 'image-test-progress-' + str(work.directory)
    previous = st.session_state.get(state_key)
    current = (latest['id'], latest['status'])
    st.session_state[state_key] = current
    if previous == (latest['id'], 'running') and latest['status'] != 'running':
        st.rerun(scope='app')
    if latest['status'] == 'running':
        st.info('正在生成测试图…切换页面不会重复提交。')
    elif latest['status'] == 'success':
        st.success(latest['message'])
        path = work.directory / latest['image_path']
        if path.is_file():
            st.image(path.read_bytes(), width=300)
    else:
        st.warning(latest.get('message', '测试已中断，请重新测试。'))


def image_settings_panel(work):
    from lab.image_service import ImageSettings, PROVIDERS
    st.subheader('图片生成模型')
    st.caption('独立于文字模型。保存只校验配置格式；生成测试图会实际调用图片服务并产生费用。')
    current = work.images.settings
    provider = st.selectbox('图片服务商', list(PROVIDERS), index=list(PROVIDERS).index(current.provider),
                            format_func=lambda p: PROVIDERS[p][0], disabled=bool(work.active))
    defaults = current if provider == current.provider else ImageSettings(endpoint=PROVIDERS[provider][1], model=PROVIDERS[provider][2], provider=provider)
    if provider == 'seedream':
        st.caption('使用火山方舟 API Key；请填写控制台当前可用的 Seedream 模型 ID，支持文字出图和原图修改，输出带 AI 生成水印的 2K PNG。')
    with st.form('image-model-settings-' + provider):
        endpoint = st.text_input('图片 API 基础地址', value=defaults.endpoint, key='image-endpoint-' + provider)
        model = st.text_input('图片模型名称', value=defaults.model, key='image-model-' + provider)
        key = st.text_input('图片 API Key（留空保留同地址已保存密钥）', type='password')
        clear = st.checkbox('清除图片 API Key')
        timeout = st.number_input('图片调用超时（秒）', 15, 600, current.timeout, step=15)
        save = st.form_submit_button('保存图片配置', disabled=bool(work.active))
    if save:
        retained = current.api_key if provider == current.provider and endpoint.strip().rstrip('/') == current.endpoint.rstrip('/') else ''
        cfg = ImageSettings(endpoint.strip().rstrip('/'), model.strip(), '' if clear else key or retained, int(timeout), provider)
        try:
            work.configure_images(cfg)
            st.success('图片配置已保存，尚未验证出图能力。')
        except Exception as exc:
            st.error(str(exc).replace(cfg.api_key, '[redacted]') if cfg.api_key else str(exc))
    saved = work.images.settings
    unsaved = (provider != saved.provider or endpoint.strip().rstrip('/') != saved.endpoint.rstrip('/')
               or model.strip() != saved.model or int(timeout) != saved.timeout
               or (bool(key) and key != saved.api_key) or (clear and bool(saved.api_key)))
    if unsaved:
        st.caption('请先保存当前图片配置，再生成测试图。')
    if st.button('生成测试图', disabled=bool(work.active) or not work.images.configured or unsaved):
        try:
            work.start('image_test', {})
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    image_test_status(work)


def render(engine, route):
    with st.sidebar:
        st.caption("当前知识库")
        st.markdown("**" + engine.settings.workspace + "**")
        st.caption("● 抽取模型已配置" if engine.workbench.llm.configured else "○ 抽取模型待配置")
        st.caption("● 中文向量模型已就绪")
        st.caption("● Qdrant 本地引擎" if engine.settings.qdrant_mode == "local" else "● Qdrant Server")
        st.divider()
        st.caption("搜集资料 · 探索知识 · 生成方案")
    from lab.ontology.ui import semantic_page
    from lab.campaign_ui import campaign_page
    from lab.demand_ui import demand_page
    try:
        {"demand": demand_page, "campaign": campaign_page, "semantic": semantic_page, "collect": collect_page, "graph": graph_page, "vectors": vector_page, "settings": settings_page}[route](engine)
    except Exception as exc:
        from lab.engine import safe_error
        st.error(safe_error(exc, engine.settings))
    st.markdown('<div class="footer">ATLAS · 让文化知识有迹可循</div>', unsafe_allow_html=True)
