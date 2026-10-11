"""Native Streamlit UI for evidence-backed topic discovery and campaigns."""
from datetime import datetime
from html import escape
import json
from zoneinfo import ZoneInfo

import streamlit as st
from lab.campaigns import export_markdown
from lab.campaign_content import clean_text
from lab.campaign_view import render_content
from lab.campaign_brand import FIELDS
from lab.trends import DATE_CHECK_VERSION, snapshot_time

LABELS = {'running': '执行中', 'success': '已完成', 'partial': '部分完成', 'failed': '失败', 'interrupted': '已中断'}


def generation_blockers(topic, match, configured, active, strict=False):
    reasons = []
    if not configured:
        reasons.append('尚未配置模型，请先在模型设置中连接模型。')
    if active:
        reasons.append('当前工作区仍有任务执行中，完成后才能生成。')
    if not strict:
        return reasons
    if topic['date_status'] == 'excluded':
        reasons.append('话题日期不在本次搜索范围内，不能作为近期热点生成方案。')
    elif topic['date_status'] != 'verified':
        reasons.append('话题日期尚未核实，品牌匹配与热点方案生成暂未开放。查看推荐依据不会解除此限制。')
    elif match.get('grade') == '不建议':
        reasons.append('本话题的匹配结论为“不建议”，请选择推荐或备选话题。')
    elif match.get('grade') not in ('推荐', '备选'):
        reasons.append('品牌匹配尚未完成，请重试失败阶段或继续已中断的任务。')
    return reasons


def local_time(value, precision=None):
    if not value:
        return '时间待核实'
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d' if precision == 'day' else '%Y-%m-%d %H:%M')


def launch(engine, kind, inputs):
    try:
        inputs = {**inputs, 'mode': 'demo'}
        _, job = engine.workbench.start(kind, inputs)
        identifier = job.snapshot()['campaign_id']
        st.query_params['campaign'] = identifier
        st.session_state['campaign-pending-history'] = identifier
    except Exception as exc:
        from lab.engine import safe_error
        message = safe_error(exc, engine.settings)
        key = engine.workbench.llm.settings.api_key
        st.error(message.replace(key, '[redacted]') if key else message)
        return
    st.rerun()


def campaign_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    header('让热点，成为品牌的好故事', '输入需求，自动发现热点、推理品牌与产品信息，再生成传播方案。', 'ATLAS / CAMPAIGN STUDIO')
    with st.expander('创建营销方案 · 输入一句需求即可开始', expanded=not bool(work.campaigns.runs())):
        st.caption('演示模式 · 日期和资料不足时仍可生成完整方案，品牌、产品及预算可由模型提出策划假设。')
        with st.form('campaign-request'):
            request = st.text_area('营销需求', placeholder='帮我搜索一下近期的热点信息并设计营销传播方案。', max_chars=2000)
            a, b = st.columns(2)
            days = a.selectbox('时间范围', [1, 7, 30], index=1, format_func=lambda d: '最近 24 小时' if d == 1 else f'最近 {d} 天')
            goal = b.selectbox('营销目标', ['品牌曝光', '用户互动', '引流', '转化'])
            with st.expander('更多条件'):
                channels = st.text_input('传播渠道', placeholder='自动选择两个合适渠道')
                budget = st.text_input('预算', placeholder='不填写时使用 3 万元演示预算')
            submitted = st.form_submit_button('搜索并生成方案', type='primary', width='stretch', disabled=work.active is not None or not work.llm.configured)
            st.caption('品牌名称、产品卖点、目标人群等由模型根据热点和知识库自动推理，无需填写或确认。需求及相关资料将发送至已配置模型。')
    if not work.llm.configured:
        st.info('请先在模型设置中连接模型，再开始搜索与策划。')
        st.link_button('打开模型设置', '/?view=settings')
    if submitted:
        launch(engine, 'campaign_search', {'request': request, 'days': days, 'goal': goal, 'channels': channels, 'budget': budget})
    campaign_results(engine)


def render_plan(plan, schedule=False):
    if plan.get('content_version') == 2:
        render_content(plan, execution=schedule)
        return
    if isinstance(plan.get('title'), str):
        st.subheader(plan['title'])
    sections = plan.get('sections', [])
    for section in sections if isinstance(sections, list) else []:
        if not isinstance(section, dict) or (schedule and section.get('name') != '执行排期'):
            continue
        st.markdown('**' + escape(str(section.get('name', ''))) + '**')
        for item in section.get('items', []) if isinstance(section.get('items'), list) else []:
            if isinstance(item, dict):
                st.write(clean_text(str(item.get('text', ''))))
    if plan.get('raw_draft'):
        st.text(plan['raw_draft'])
    if not schedule and isinstance(plan.get('assumptions'), list):
        st.caption('假设与待确认：' + '；'.join(str(a) for a in plan['assumptions']))


def show_evidence(service, run, topic, version):
    match = run['matches'].get(topic['id'])
    if match:
        for title, key in [('品牌关联', 'brand_fit'), ('人群关联', 'audience_fit'), ('传播切口', 'angle'), ('执行限制', 'constraints')]:
            st.write(f"{title}：{match[key]}")
        for conflict in match['conflicts']:
            st.warning('资料冲突（不作为既定事实）：' + conflict)
    evidence = version['evidence'] if version else service.evidence(run, topic)
    for key, item in evidence.items():
        with st.expander(f"[{key}] {item.get('title') or item.get('file_path') or item.get('kind') or '品牌依据'}"):
            if item.get('url'):
                st.link_button('打开原始网页', item['url'])
                date = run['extractions'].get(key, {}).get('date', {})
                st.caption(f"发布时间：{local_time(date.get('published_at'), date.get('published_precision'))} · 事件时间：{local_time(date.get('event_at'), date.get('event_precision'))}")
                st.caption(f"采集时间：{local_time(item.get('collected_at'))} 北京时间")
                st.write(date.get('reason', ''))
                for ev in date.get('evidence', []):
                    st.text(ev['quote'])
                st.text(item.get('text', ''))
                if item.get('analysis_truncated'):
                    st.caption('分析正文截取至前 9,000 字符。')
                for alias in item.get('aliases', []):
                    st.link_button('相同正文来源：' + alias.get('title', alias['url']), alias['url'])
            elif key == 'B1':
                st.json(item['content'])
            else:
                st.text(item.get('content', ''))
                st.caption('片段 ID：' + str(item.get('chunk_id', '')))
                st.link_button('返回资料工作台', '/?view=collect')
    if not topic.get('knowledge') and not run.get('brand_knowledge') and topic['id'] != 'evergreen':
        st.caption('未找到相关知识库片段；自动品牌与产品方向作为策划设定使用。' if run.get('brand_profile') else '未找到相关知识库片段，品牌匹配使用历史简档。')


def show_brand_profile(run):
    profile = run.get('brand_profile')
    if not profile:
        return
    with st.expander('自动推理的品牌与产品'):
        kinds = {item['field']: item['kind'] for item in profile['basis']}
        for field, label in FIELDS.items():
            st.write(f"{label}：{profile[field]}")
            st.caption(kinds[field])
        st.caption('已自动用于本次策划。资料提取保留来源；策划推断是建议方向，不代表已存在的品牌或产品属性。')
    with st.expander('查看品牌推理依据'):
        for item in profile['basis']:
            st.write(f"{FIELDS[item['field']]}：{item['reason']}")
            for citation in item['citations']:
                st.text(f"[{citation['ref']}] {citation['quote']}")


@st.fragment(run_every=2)
def campaign_results(engine):
    service = engine.workbench.campaigns
    runs = service.runs()
    if not runs:
        st.info('只需输入需求。系统会先搜索热点，再自动推理品牌名称、产品卖点和目标人群，直接生成方案。')
        return
    identifiers = [r['id'] for r in runs]
    pending = st.session_state.pop('campaign-pending-history', None)
    if pending in identifiers:
        st.session_state['campaign-history'] = pending
    requested = st.query_params.get('campaign')
    if requested in identifiers and st.session_state.get('campaign-url-seen') != requested:
        st.session_state['campaign-history'] = requested
        st.session_state['campaign-url-seen'] = requested
    if st.session_state.get('campaign-history') not in identifiers:
        st.session_state['campaign-history'] = requested if requested in identifiers else identifiers[0]
    # Keep labels stable while background inference updates the brand profile.
    names = {r['id']: f"{local_time(r['created_at'])} · {r['inputs']['request'][:35]} · {r['id'][:6]}" for r in runs}
    identifier = st.selectbox('历史任务', identifiers, index=identifiers.index(st.session_state['campaign-history']), format_func=lambda r: names[r], key='campaign-history')
    run = service.load(identifier)
    op = list(run['operations'].values())[-1]
    active = engine.workbench.active is not None
    busy = active or not engine.workbench.llm.configured
    previous_busy = st.session_state.get('campaign-was-busy', False)
    st.session_state['campaign-was-busy'] = active
    if previous_busy and not active:
        st.rerun()
    topics = run.get('topics', [])
    if op['status'] == 'running':
        st.info('正在' + op['stage'] + '，完成后会自动显示结果。')
    elif op['status'] in ('failed', 'interrupted'):
        st.warning(op.get('message', '任务未完成，展开任务详情可以重试。'))
    with st.expander('任务详情与资料记录'):
        st.caption(f"资料快照 {local_time(snapshot_time(run))} 北京时间 · 最近 {run['inputs']['days']} 天 · {LABELS[op['status']]}")
        st.write('理解需求 → 搜集资料 → 推理品牌与产品 → 品牌匹配 → 生成方案')
        st.caption(f"当前阶段：{op['stage']} · 已读取 {len(run['sources'])} 份正文 · 已提取 {len(run['extractions'])} 份 · 已匹配 {len(run['matches'])} 个话题")
        if op['status'] == 'running':
            st.info('任务在后台执行，切换页面后继续。请保持服务运行。')
        elif op['status'] in ('failed', 'interrupted'):
            st.warning(op.get('message', '任务未完成'))
            exhausted = len(op.get('attempts', [])) >= 2 and bool(op['attempts'][-1].get('errors'))
            if not exhausted and st.button('重试失败阶段', key='retry-' + op['id'], disabled=busy):
                launch(engine, 'campaign_retry', {'run_id': identifier, 'operation_id': op['id']})
        if run['issues']:
            with st.expander('资料缺口与失败记录'):
                for issue in run['issues']:
                    st.write(issue)
        can_recheck = bool(run.get('collection_complete') and run.get('extractions') and run.get('date_check_version', 0) < DATE_CHECK_VERSION)
        if can_recheck:
            st.info('此任务使用旧版日期校验。可复用已采集资料重新核验，随后自动继续品牌推理、匹配和方案生成，无需重新搜索。')
            if st.button('重新核验日期并继续', key='recheck-' + identifier, disabled=busy):
                launch(engine, 'campaign_recheck', {'run_id': identifier})
        if run.get('recheck_of'):
            st.caption('已复用原任务的资料快照，原任务和方案版本保持不变。')
        with st.expander('执行记录与恢复'):
            for operation in reversed(list(run['operations'].values())):
                st.write(f"{local_time(operation['created_at'])} · {operation['stage']} · {LABELS[operation['status']]}")
                if operation.get('message'):
                    st.caption(operation['message'])
                exhausted = len(operation.get('attempts', [])) >= 2 and bool(operation['attempts'][-1].get('errors'))
                if operation['id'] != op['id'] and operation['status'] in ('failed', 'interrupted') and not exhausted:
                    if st.button('重试此操作的失败阶段', key='retry-history-' + operation['id'], disabled=busy):
                        launch(engine, 'campaign_retry', {'run_id': identifier, 'operation_id': operation['id']})
            jobs = [job for job in engine.workbench.jobs() if job.get('campaign_id') == identifier]
            if jobs:
                st.json(jobs[0]['events'], expanded=False)
        with st.expander('本次需求快照'):
            st.json(run['inputs'])
        excluded = [(sid, e) for sid, e in run['extractions'].items() if e['date']['status'] != 'verified']
        if excluded:
            with st.expander('时间待核实或已排除的资料'):
                for sid, e in excluded:
                    st.write(f"[{sid}] {e['title']}：{e['date']['reason']}")
                    st.json(e['date'])
    if not topics and op['status'] != 'running':
        st.info('本轮没有采集到话题，可继续生成常规内容演示方案。')
    if any(v['topic_id'] == 'evergreen' for v in run['versions']):
        topics = topics + [{'id': 'evergreen', 'title': '常规内容建议（非热点）', 'source_ids': [], 'date_status': 'evergreen', 'date': ''}]
    left, right = st.columns([1, 2.4], gap='medium')
    selected_key = 'campaign-topic-' + identifier
    selected = st.session_state.get(selected_key)
    if selected not in [t['id'] for t in topics]:
        selected = op.get('topic_id') if op.get('topic_id') in [t['id'] for t in topics] else (topics[0]['id'] if topics else None)
    with left, st.container(key='campaign-topics'):
        st.subheader('话题候选')
        st.caption('选择话题，查看对应方案与依据。')
        for topic in topics:
            match = run['matches'].get(topic['id'], {})
            with st.container(border=True):
                st.caption(match.get('grade', '超出时间范围' if topic['date_status'] == 'excluded' else '时间待核实' if topic['date_status'] == 'unverified' else '等待匹配' if topic['id'] != 'evergreen' else '常规建议'))
                if st.button(topic['title'], key=identifier + topic['id'], type='primary' if selected == topic['id'] else 'secondary', width='stretch'):
                    st.session_state[selected_key] = topic['id']
                    st.rerun()
                st.caption(('时间：' + local_time(topic.get('date'), topic.get('date_precision'))) if topic['id'] != 'evergreen' else '不依赖热点时效')
                if topic['id'] != 'evergreen':
                    st.caption('日期已有依据' if topic['date_status'] == 'verified' else '超出搜索时间范围，可作创意参考' if topic['date_status'] == 'excluded' else '日期待核实，可继续生成演示方案')
                angle = clean_text(match.get('angle', ''))
                st.markdown('<p class="topic-preview">' + escape(angle) + '</p>', unsafe_allow_html=True)
                st.caption(f"{len(topic['source_ids'])} 份不同正文 · 热度未验证" if topic['id'] != 'evergreen' else '非热点方案')
        if op['status'] != 'running' and st.button('生成常规内容建议（非热点）', disabled=busy, key='evergreen-' + identifier):
            launch(engine, 'campaign_evergreen', {'run_id': identifier})
    topic = next((t for t in topics if t['id'] == selected), None)
    if not topic:
        return
    with right, st.container(key='campaign-document'):
        st.caption('当前选题 · ' + topic['title'])
        versions = [v for v in run['versions'] if v['topic_id'] == selected]
        version = None
        if versions:
            by_id = {v['id']: v for v in versions}
            version_id = st.selectbox('方案版本', list(by_id), index=len(versions) - 1, format_func=lambda vid: f"版本 {by_id[vid]['number']} · {('演示方案' if by_id[vid].get('mode') == 'demo' else '已核验') if by_id[vid]['status'] == 'complete' else '未完成草稿'}", key='version-' + identifier + selected + '-' + str(len(versions)))
            version = by_id[version_id]
        if version and version['status'] == 'complete':
            st.success('方案已生成')
            if run['issues']:
                st.caption('部分资料未能读取，现有方案已完成；详情见资料记录。')
        if version and version['status'] != 'complete':
            if st.button('继续完善', key='complete-' + version['id'], disabled=busy):
                launch(engine, 'campaign_revise', {'run_id': identifier, 'topic_id': selected, 'version_id': version['id'], 'revision': '请补齐当前草稿缺失的文案、脚本、排期和预算，保留已有有效内容，输出完整新版演示方案。'})
        elif version and version['plan'].get('content_version') != 2:
            if st.button('生成新版演示方案', key='upgrade-' + version['id'], disabled=busy):
                launch(engine, 'campaign_revise', {'run_id': identifier, 'topic_id': selected, 'version_id': version['id'], 'revision': '将当前方案升级为完整新版演示方案：提炼策略，提供两篇可发布图文、一条完整视频脚本、逐日排期和具体预算。'})
        plan_tab, schedule_tab, evidence_tab = st.tabs(['营销方案', '执行安排', '推荐依据'])
        with plan_tab:
            if version:
                if version.get('mode') == 'demo':
                    st.caption('演示方案 · 已自动补充策划假设，内容未经过事实复核，可继续调整或导出。')
                if version['errors']:
                    st.warning('方案还有内容待完善，已保留以下草稿。')
                    for error in version['errors']:
                        st.write(error)
                render_plan(version['plan'])
            else:
                match = run['matches'].get(selected, {})
                blockers = generation_blockers(topic, match, engine.workbench.llm.configured, active)
                if blockers:
                    for reason in blockers:
                        st.warning(reason)
                else:
                    st.info('可直接生成完整演示方案，系统会自动补充品牌、产品和传播内容。')
                    if topic['date_status'] != 'verified':
                        st.caption('日期尚未确认或超出搜索范围，将作为创意参考，不影响生成。')
                    if match.get('grade') == '不建议':
                        st.caption('品牌关联较弱，模型仍会给出演示方案与调整建议。')
                if st.button('为此话题生成方案', disabled=bool(blockers), key='generate-' + identifier + selected):
                    launch(engine, 'campaign_generate', {'run_id': identifier, 'topic_id': selected})
        with evidence_tab:
            show_brand_profile(run)
            show_evidence(service, run, topic, version)
        with schedule_tab:
            if version:
                render_plan(version['plan'], schedule=True)
            else:
                st.caption('生成方案后显示执行排期。')
        if version:
            with st.form('revise-' + identifier + selected):
                revision = st.text_area('继续调整这份方案', placeholder='例如：预算控制在 1 万元，增加小红书内容', max_chars=2000)
                revise = st.form_submit_button('生成新版本', disabled=busy, type='primary')
            if revise:
                launch(engine, 'campaign_revise', {'run_id': identifier, 'topic_id': selected, 'version_id': version['id'], 'revision': revision})
            a, b = st.columns(2)
            filename = f"campaign-{identifier[:8]}-v{version['number']}"
            a.download_button('下载 Markdown', width='stretch', data=export_markdown(run, version), file_name=filename + '.md', mime='text/markdown', key='md-' + version['id'])
            b.download_button('下载 JSON', width='stretch', data=json.dumps({'schema_version': run.get('schema_version', 1), 'workspace': run['workspace'], 'inputs': run['inputs'], 'snapshot_at': snapshot_time(run), 'version': version}, ensure_ascii=False, indent=2), file_name=filename + '.json', mime='application/json', key='json-' + version['id'])
