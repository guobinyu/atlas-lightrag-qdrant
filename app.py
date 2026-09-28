import atexit
from html import escape
import json
import time

import streamlit as st

from lab.charts import scatter, short_title
from lab.config import ROOT, load_settings
from lab.engine import Engine, safe_error
from lab.trace import list_runs, replay_state
from lab.workbench_ui import navigation, render


st.set_page_config(page_title="Atlas · 知识工作台", page_icon="◈", layout="wide")
st.markdown((ROOT / "assets/style.css").read_text(encoding="utf-8"), unsafe_allow_html=True)


@st.cache_resource
def get_engine(settings):
    engine = Engine(settings)
    atexit.register(engine.close)
    return engine


def timeline(events):
    by_type = {event["event_type"]: event for event in events}
    stages = [
        ("01", "问题向量化", "embedding.started", "embedding.completed"),
        ("02", "提交 Qdrant", "vector_search.started", "vector_search.completed"),
        ("03", "返回匹配", "vector_search.started", "vector_search.completed"),
        ("04", "整理上下文", "vector_search.completed", "context.completed"),
    ]
    cards = []
    failed = "query.failed" in by_type
    for number, title, begin, end in stages:
        complete = end in by_type
        running = begin in by_type and not complete
        state = "done" if complete else "error" if failed and running else "active" if running else "pending"
        detail = "等待执行"
        if complete:
            event = by_type[end]
            if number == "03":
                detail = f"{event['data']['count']} 条真实命中"
            elif number == "04":
                detail = f"{event['data']['count']} 个片段 · {event['duration_ms']:.1f} ms"
            else:
                detail = f"{event['duration_ms']:.1f} ms"
                if number == "01":
                    detail = f"{event['data']['dimension']} 维 · " + detail
        elif running:
            detail = "执行失败" if failed else "正在执行…"
        cards.append(f'<div class="phase {state}"><div class="phase-number">{number}</div><div><strong>{title}</strong><small>{detail}</small></div><span class="phase-dot"></span></div>')
    return '<div class="timeline">' + "".join(cards) + "</div>"


def set_record(record, history=False):
    st.session_state.record = record
    st.session_state.selected_id = record.get("hits", [{}])[0].get("point_id") if record.get("hits") else None
    st.session_state.viewing_history = history
    st.session_state.replaying = False
    st.session_state.replay_step = len(record["events"])
    st.session_state.pop("replay_slider", None)


try:
    settings = load_settings()
    engine = get_engine(settings)
    with st.spinner("正在准备知识库和本地模型，首次下载可能需要一些时间…"):
        info = engine.ready.result(timeout=600)
except Exception as exc:
    st.title("检索观察台")
    st.error(safe_error(exc, settings) if "settings" in locals() else str(exc))
    st.info("请检查项目中的 .env 与 README。首次启动需要网络下载模型，配置修改后请重启应用。")
    st.stop()

route = navigation()
if route != "retrieve":
    render(engine, route)
    st.stop()

runs = list_runs(settings.run_dir)
if "record" not in st.session_state:
    if runs:
        set_record(runs[0], history=True)
    else:
        st.session_state.update(record=None, selected_id=None, replaying=False, viewing_history=False)
record = st.session_state.record

with st.sidebar:
    st.markdown('<div class="side-label">当前知识库</div>', unsafe_allow_html=True)
    st.markdown(f"**{escape(settings.workspace)}**")
    mode_label = "Qdrant 本地引擎" if settings.qdrant_mode == "local" else "Qdrant 服务"
    st.caption(f"● {mode_label} · Cosine")
    st.caption(f"{engine.total_points} 个文本向量 · {settings.embedding_dim} 维")
    st.divider()
    st.markdown('<div class="side-label">试一个问题</div>', unsafe_allow_html=True)
    examples = ["缂丝为什么称为通经断纬？", "云锦、宋锦和蜀锦有什么区别？", "丝绸之路怎样促进文化交流？", "丝绸为什么要避光保存？"]
    for i, example in enumerate(examples):
        if st.button(example, key=f"example-{i}", width="stretch", type="tertiary"):
            st.session_state.question = example
    st.divider()
    st.markdown('<div class="side-label">历史检索</div>', unsafe_allow_html=True)
    if runs:
        history = st.selectbox("选择已保存的记录", runs, format_func=lambda r: f"{r['created_at'][11:19]} · {r['question'][:18]}", label_visibility="collapsed")
        if st.button("载入记录", width="stretch", icon=":material/history:"):
            set_record(history, history=True)
            st.rerun()
        st.caption("只读取本地记录；不会再次检索。")
    else:
        st.caption("完成一次检索后，可在这里回看。")
    st.divider()
    with st.expander("连接与数据"):
        st.caption("模型")
        st.code(settings.embedding_model, language=None, wrap_lines=True)
        st.caption("Collection")
        st.code(engine.collection, language=None, wrap_lines=True)
        st.caption("当前仅运行文本块检索，不生成回答。")
        if settings.qdrant_mode == "local":
            st.caption("本地引擎使用持久化文件，不提供独立 Qdrant Dashboard。接入 Qdrant Server 后可使用官方管理界面。")
        else:
            st.link_button("打开 Qdrant Dashboard", settings.qdrant_url.rstrip("/") + "/dashboard")
        if st.button("刷新向量背景", width="stretch"):
            try:
                engine.refresh_projection().result(timeout=60)
                st.toast("已重新读取并投影当前工作区；历史图保留原始快照。")
            except Exception as exc:
                st.error(safe_error(exc, settings))
        st.caption("接入已有 LightRAG 工作区或导入资料，请查看项目 README。")

st.markdown('<div class="eyebrow">RETRIEVAL OBSERVATORY <span>真实调用 · 可回放</span></div>', unsafe_allow_html=True)
st.markdown('<h1>看见一次语义检索</h1><p class="subtitle">从问题向量到相关片段，把 LightRAG 与 Qdrant 的检索过程展开给你看。</p>', unsafe_allow_html=True)

with st.container(key="query-panel"):
    with st.form("query-form", border=False):
        st.text_input("输入问题", value=examples[0], key="question", placeholder="你想从知识库中找到什么？", max_chars=2000)
        a, b, c = st.columns([1, 2.2, 1.2], vertical_alignment="bottom")
        with a:
            k = st.number_input("最多返回 Top-K", min_value=1, max_value=20, value=5, step=1)
        with b:
            threshold = st.slider("最低相似度 · Cosine", min_value=-1.0, max_value=1.0, value=0.2, step=0.01)
        with c:
            submitted = st.form_submit_button("开始检索", type="primary", width="stretch", icon=":material/search:")
    st.caption("LightRAG naive · 本地中文模型" if settings.embedding_provider == "fastembed" else "LightRAG naive · 已配置的 Embedding 服务")

live_area = st.empty()
if submitted:
    try:
        future, trace = engine.submit(st.session_state.question, int(k), threshold)
        live_events = []
        while not future.done():
            while not trace.queue.empty():
                live_events.append(trace.queue.get_nowait())
            live_area.markdown(timeline(live_events), unsafe_allow_html=True)
            time.sleep(0.06)
        set_record(future.result())
        st.rerun()
    except Exception as exc:
        st.error(safe_error(exc, settings))

record = st.session_state.record
if record:
    bar_left, bar_right = st.columns([4, 1], vertical_alignment="center")
    with bar_left:
        label = "历史记录" if st.session_state.get("viewing_history") else "本次检索"
        st.markdown(f'<div class="run-line"><span class="run-tag">{label}</span> {escape(record["question"])} <small>#{record["query_id"][:8]}</small></div>', unsafe_allow_html=True)
    with bar_right:
        replaying = st.toggle("逐步回放", key="replaying")
    if replaying:
        st.caption("拖动步骤查看已记录的过程，回放不会调用模型或数据库。")
        step = st.slider("回放步骤", 0, len(record["events"]), value=len(record["events"]), key="replay_slider", label_visibility="collapsed")
    else:
        step = len(record["events"])
    state = replay_state(record, step)
    st.markdown(timeline(state["events"]), unsafe_allow_html=True)
    if record["status"] == "failed":
        st.error(record["error"])
    if record.get("visualization_error"):
        st.warning("检索已完成，但向量图读取失败：" + record["visualization_error"])
    plot = record.get("plot", {"points": engine.background, "query": None, "variance": engine.projection.variance})
    hits = record.get("hits", [])
else:
    state = {"show_query": False, "show_hits": False, "show_context": False, "events": []}
    st.markdown(timeline([]), unsafe_allow_html=True)
    plot = {"points": engine.background, "query": None, "variance": engine.projection.variance, "version": engine.projection_version}
    hits = []

left, right = st.columns([1.75, 1], gap="large")
with left, st.container(key="plot-panel"):
    title, control = st.columns([3, 1], vertical_alignment="center")
    with title:
        st.markdown('<div class="panel-heading">向量空间 <span>2D / PCA</span></div>', unsafe_allow_html=True)
    with control:
        links = st.toggle("匹配连线", value=False, help="只表示问题和返回结果的关系，不代表 HNSW 遍历路径。")
    chart_key = f"scatter_{record['query_id'] if record else 'background'}_{step if record else 0}"
    def select_point():
        event = st.session_state.get(chart_key, {})
        points = event.get("selection", {}).get("points", [])
        if points:
            chosen = points[-1].get("customdata", [None])[0]
            if chosen and chosen != "__query__":
                st.session_state.selected_id = chosen
    fig = scatter(plot, hits, st.session_state.selected_id, state["show_query"], state["show_hits"], links)
    st.plotly_chart(fig, key=chart_key, on_select=select_point, selection_mode="points", width="stretch", theme=None,
                    config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"], "scrollZoom": False})
    sample_count = plot.get("background_count", len(plot.get("points", [])))
    total = record["config"]["total_points"] if record else engine.total_points
    st.caption(f"背景样本 {sample_count} / {total} · PCA 保留方差 {plot.get('variance', 0):.1%} · 点击圆点查看片段")
    st.markdown('<div class="plot-note">二维距离可能失真，排名以原始高维相似度为准。灰点仅表示未在本次响应中返回。</div>', unsafe_allow_html=True)
    if plot.get("note"):
        st.caption(plot["note"])

with right, st.container(key="results-panel"):
    visible_hits = hits if state["show_hits"] else []
    st.markdown(f'<div class="panel-heading">匹配排名 <span>{len(visible_hits):02d} RESULTS</span></div>', unsafe_allow_html=True)
    if record:
        st.caption(f"本记录 Top-K {record['top_k']} · 阈值 {record['threshold']:.2f} · Cosine 越大越相似")
    if not visible_hits:
        if record and state["show_hits"]:
            st.info("没有符合本次条件的结果。可降低阈值，或换一个问题重新检索。")
        else:
            st.info("开始检索或推进回放，查看真实匹配排名。")
    for hit in visible_hits:
        selected = hit["point_id"] == st.session_state.selected_id
        title = short_title(hit["content"], 18)
        if st.button(f"{hit['rank']:02d}　{title}　{hit['score']:.4f}", key=f"hit-{record['query_id']}-{hit['point_id']}", width="stretch", type="primary" if selected else "secondary"):
            st.session_state.selected_id = hit["point_id"]
            st.rerun()
    point = next((p for p in plot.get("points", []) if p["point_id"] == st.session_state.selected_id), None)
    hit = next((h for h in visible_hits if h["point_id"] == st.session_state.selected_id), None)
    if point:
        st.markdown('<div class="detail-rule"></div>', unsafe_allow_html=True)
        if hit and state["show_context"]:
            st.caption("✓ 已进入最终上下文" if hit["in_context"] else "已返回 · 未进入最终上下文")
        elif hit:
            st.caption("Qdrant 已返回 · 等待上下文整理")
        else:
            st.caption("背景样本 · 当前步骤未返回此点")
        st.markdown(f"**{escape(short_title(point['content'], 50))}**")
        st.caption(point["file_path"])
        with st.container(height=210, border=False):
            st.text(point["content"])
        with st.expander("查看 ID"):
            st.caption("Qdrant point ID")
            st.code(point["point_id"], language=None, wrap_lines=True)
            st.caption("LightRAG chunk ID")
            st.code(point["chunk_id"] or "无业务 ID", language=None, wrap_lines=True)

if record:
    st.markdown('<div class="section-space"></div>', unsafe_allow_html=True)
    with st.container(key="evidence-panel"):
        context_tab, request_tab, event_tab = st.tabs(["最终上下文", "实际请求与向量", "事件记录"])
        with context_tab:
            if state["show_context"]:
                chunks = record.get("context", {}).get("data", {}).get("chunks", [])
                st.caption(f"LightRAG 实际采用 {len(chunks)} 个文本块 · 预算 8,000 tokens · 不生成最终回答")
                for i, chunk in enumerate(chunks, 1):
                    with st.expander(f"{i:02d} · {chunk.get('file_path', '未知来源')} · {short_title(chunk['content'], 42)}", expanded=i == 1):
                        st.text(chunk["content"])
                        st.caption(chunk["chunk_id"])
                if not chunks:
                    st.info("本次没有可用的检索上下文。")
            else:
                st.caption("回放到「整理上下文」后显示本次实际采用片段。")
        with request_tab:
            if any(e["event_type"] == "vector_search.started" for e in state["events"]):
                col1, col2 = st.columns(2)
                with col1:
                    st.caption("LightRAG 发出的真实 Qdrant 请求")
                    st.json(record.get("request", {}), expanded=True)
                    st.caption("search_params 未出现表示调用未指定，使用引擎默认值。")
                with col2:
                    embedding = next((e for e in state["events"] if e["event_type"] == "embedding.completed"), None)
                    if embedding:
                        st.caption("本次 Embedding 输出 · 前 8 个分量")
                        st.json(embedding["data"], expanded=True)
                    st.caption(f"Embedding 调用 {record['embedding_calls']} 次 · 相似度查询 {record['search_calls']} 次")
                    if record.get("query_duration_ms") is not None:
                        st.caption(f"LightRAG 检索总耗时 {record['query_duration_ms']:.1f} ms；可视化补读/投影 {plot.get('read_ms', 0):.1f} ms 单独计算。")
                    if record["config"]["qdrant_mode"] == "local":
                        st.info("本记录使用 Qdrant Client 的本地精确检索引擎。连接 Server 后由服务端决定索引执行方式。")
            else:
                st.caption("回放到「提交 Qdrant」后展示请求。")
        with event_tab:
            st.dataframe([{"步骤": e["seq"], "事件": e["event_type"], "开始/完成时间 (UTC)": e["started_at"], "距开始 (ms)": e["elapsed_ms"], "调用耗时 (ms)": e["duration_ms"]} for e in state["events"]], hide_index=True, width="stretch")
            st.download_button("导出完整检索记录", json.dumps(record, ensure_ascii=False, indent=2), file_name=f"retrieval-{record['query_id'][:8]}.json", mime="application/json")
            st.caption("记录包含问题、原文与参数，保存在本机 data/runs。完整高维向量和 API Key 不进入回放文件。")

st.markdown('<div class="footer">LIGHTRAG 1.5.7 × QDRANT · 真实调用轨迹，不提供数据库内部 HNSW 访问路径。</div>', unsafe_allow_html=True)
