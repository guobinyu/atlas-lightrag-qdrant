"""Hotspot prediction workbench: one-way five steps, one step open at a time, one primary action per step."""
from copy import deepcopy
from html import escape
import json

import streamlit as st

from lab import demand_scoring
from lab.charts import score_breakdown, score_contributions
from lab import flow_ui as flow
from lab.demand_ui import (TOPIC_KINDS, action, candidate_cards, capacity_form, confirmation_view, enterprise_picker,
                           evidence_view, inline_questions, launch, manual_topic_form, round_topics_caption, scoring_form,
                           step_feedback, task_select)
from lab.demands import can_answer, current_brief, current_design, round_markdown, stale, structured_lines, topic_note
from lab.enterprises import Enterprises
from lab.flow_ui import NUMBERS, brief_content, current_round, direction_lines, last_op, latest_analysis
from lab.ui_kit import badges, boundary

STEPS = ['需求与产能', '选题', '候选方向', '方案包', '设计与纹样']
PREFIX = 'predict'
AWAIT = 'predict-await'
# Changing step i makes results from this point on historical.
IMPACT = ('rounds', 'rounds', 'selection', 'brief')


def edit_key(run, i):
    return flow.edit_key(PREFIX, run, i)


def flash(message):
    flow.flash(PREFIX, message)


def impact(run, i):
    flow.impact(run, IMPACT[i])


def flow_state(run, advanced=False):
    """(done flags, index of the current step or None, current brief, current design). Pure; used by tests.
    advanced: the user moved past the package step; any design for the current package counts as well."""
    brief = current_brief(run)
    design = current_design(run, brief)
    designed = bool(brief) and any(v['brief_id'] == brief['id'] for v in run.get('design_versions', []))
    done = [run['approved_revision'] == run['revision'], bool(run['topic_ids']), bool(run['selection']),
            bool(brief) and (advanced or designed), bool(design)]
    for i in range(1, len(done)):
        done[i] = done[i] and done[i - 1]
    return done, next((i for i, d in enumerate(done) if not d), None), brief, design


# ① 需求与产能

def close_new_task():
    st.session_state.pop('predict-new-open', None)


@st.dialog('新建研判任务', width='large', on_dismiss=close_new_task)
def new_task_dialog(engine):
    work = engine.workbench
    service = work.demands
    profile_id, fields = enterprise_picker(engine, 'predict-new-profile', False, '企业档案')
    key = f'predict-new-{profile_id}-'
    request = st.text_area('研判需求', placeholder='例如：结合未来的节日与消费节点，预测适合批量上市的丝绸产品。', max_chars=4000, key=key + 'request')
    a, b = st.columns(2)
    industry = a.text_input('行业（必填）', fields.get('industry', ''), max_chars=4000, key=key + 'industry')
    brand = b.text_input('品牌名称（选填）', fields.get('brand', ''), max_chars=4000, key=key + 'brand')
    capabilities = st.text_area('生产能力（必填）', fields.get('capabilities', ''), max_chars=4000, key=key + 'capabilities')
    constraints = st.text_area('已知限制（选填）', fields.get('constraints', ''), max_chars=4000, key=key + 'constraints')
    capacity, budget = capacity_form({'capacity': fields.get('capacity', {})}, key, False)
    days = st.selectbox('近期热点搜索范围', [1, 7, 30], index=1, format_func=lambda n: '最近 24 小时' if n == 1 else f'最近 {n} 天', key=key + 'days')
    st.caption('创建后模型会先读一遍需求；有不确定的地方会在页面里直接追问，没有就自动确认。')
    if st.button('创建并分析需求', type='primary', key=key + 'create'):
        ok, run = action(engine, service.create, {'request': request, 'industry': industry, 'brand': brand, 'capabilities': capabilities,
                                                  'constraints': constraints, 'capacity': capacity, 'budget': budget, 'path': 'trend', 'days': days})
        if not ok:
            return
        close_new_task()
        st.query_params.clear()
        st.query_params.update({'view': 'predict', 'demand': run['id']})
        if work.llm.configured:
            ok, result = action(engine, work.start, 'demand_analyze', {'run_id': run['id']})
            if ok:
                st.session_state[AWAIT] = {'id': run['id'], 'op': result[1].value['operation_id']}
        elif action(engine, service.approve, run['id'])[0]:
            flash('任务已创建并确认需求。配置文字模型后可生成候选。')
        st.rerun()


def settle_analysis(engine, run, busy):
    """When an awaited analysis finishes without open questions, confirm the requirement automatically."""
    pending = st.session_state.get(AWAIT)
    if not pending or pending['id'] != run['id']:
        return
    op = run['operations'].get(pending['op'])
    if op is not None and op['status'] == 'running':
        return
    st.session_state.pop(AWAIT, None)
    if (op and op['status'] == 'success' and not busy and run['approved_revision'] != run['revision']
            and not can_answer(run, latest_analysis(run))):
        if action(engine, engine.workbench.demands.approve, run['id'])[0]:
            flash('需求已清楚，已自动确认。下一步：选题。')
            st.rerun()


def requirement_lines(inputs):
    lines = [f"行业：{inputs['industry']}" + (f" · 品牌：{inputs['brand']}" if inputs.get('brand') else ''),
             f"生产能力：{inputs['capabilities']}"]
    if inputs.get('constraints'):
        lines.append('限制：' + inputs['constraints'])
    return lines + (structured_lines(inputs) or ['未设置产能参数，候选不做产能硬性检查。'])


def need_current(engine, run, busy):
    work = engine.workbench
    analysis = latest_analysis(run)
    op = last_op(run)
    st.markdown('**需求**：' + escape(run['inputs']['request']))
    step_feedback(engine, run['id'], op, ('demand_analyze',), 'flow-need')
    if op and op['kind'] == 'demand_analyze' and op['status'] == 'running':
        return
    if analysis and can_answer(run, analysis):
        st.info('模型对需求有不确定的地方，请回答下面的问题；回答完会自动再分析一次。', icon=':material/help:')
        inline_questions(engine, run, analysis, AWAIT)
        with st.expander('跳过追问，直接确认当前需求'):
            confirmation_view(engine, run, analysis, inline=True, auto_search=False)
    elif analysis:
        confirmation_view(engine, run, analysis, inline=True, auto_search=False)
    else:
        for line in requirement_lines(run['inputs']):
            st.caption(line)
        a, b = st.columns(2)
        if a.button('分析需求', type='primary', disabled=busy or not work.llm.configured, key=f"predict-analyze-{run['id']}", width='stretch'):
            ok, result = action(engine, work.start, 'demand_analyze', {'run_id': run['id']})
            if ok:
                st.session_state[AWAIT] = {'id': run['id'], 'op': result[1].value['operation_id']}
                st.rerun()
        if b.button('直接确认需求', disabled=busy, key=f"predict-approve-{run['id']}", width='stretch'):
            if action(engine, work.demands.approve, run['id'])[0]:
                flash('需求已确认。下一步：选题。')
                st.rerun()


def need_edit(engine, run, busy):
    work = engine.workbench
    inputs = run['inputs']
    st.caption('只改产能或预算会保留已选话题；改需求、行业、能力、限制或搜索范围，会清空选题，需要重新选择。')
    impact(run, 0)
    analysis = latest_analysis(run)
    if analysis and can_answer(run, analysis):
        inline_questions(engine, run, analysis, AWAIT)
        return
    profile_id, fields = enterprise_picker(engine, f"predict-edit-profile-{run['id']}", busy, '用企业档案覆盖（选填）')
    base = {**inputs, **fields}
    key = f"predict-edit-{run['id']}-{run['revision']}-{profile_id}-"
    values = dict(inputs)
    values['request'] = st.text_area('研判需求', base['request'], max_chars=4000, key=key + 'request', disabled=busy)
    a, b = st.columns(2)
    values['industry'] = a.text_input('行业（必填）', base['industry'], max_chars=4000, key=key + 'industry', disabled=busy)
    values['brand'] = b.text_input('品牌名称（选填）', base.get('brand', ''), max_chars=4000, key=key + 'brand', disabled=busy)
    values['capabilities'] = st.text_area('生产能力（必填）', base['capabilities'], max_chars=4000, key=key + 'capabilities', disabled=busy)
    values['constraints'] = st.text_area('已知限制（选填）', base.get('constraints', ''), max_chars=4000, key=key + 'constraints', disabled=busy)
    values['capacity'], values['budget'] = capacity_form(base, key, busy)
    values['days'] = st.selectbox('近期热点搜索范围', [1, 7, 30], index=[1, 7, 30].index(inputs.get('days', 7)),
                                  format_func=lambda n: '最近 24 小时' if n == 1 else f'最近 {n} 天', key=key + 'days', disabled=busy)
    a, b, c = st.columns(3)
    if a.button('保存修改', type='primary', disabled=busy, key=key + 'save', width='stretch'):
        if action(engine, work.demands.confirm, run['id'], values, run['revision'])[0]:
            st.session_state.pop(edit_key(run, 0), None)
            flash('需求已更新并确认。')
            st.rerun()
    if b.button('重新分析需求', disabled=busy or not work.llm.configured, key=key + 'reanalyze', width='stretch'):
        ok, result = action(engine, work.start, 'demand_analyze', {'run_id': run['id']})
        if ok:
            st.session_state[AWAIT] = {'id': run['id'], 'op': result[1].value['operation_id']}
            st.rerun()
    with c.popover('另存为企业档案', icon=':material/save:', width='stretch'):
        name = st.text_input('档案名称', max_chars=60, key=key + 'profile-name')
        if st.button('保存档案', key=key + 'profile-save'):
            if action(engine, work.enterprises.save, Enterprises.from_task(name, inputs))[0]:
                st.success('已保存，可在新建任务时选择。')


# ② 选题

def topic_view(engine, run, topics, busy, editing):
    service = engine.workbench.demands
    configured = engine.workbench.llm.configured
    st.markdown(badges('score', 'heat'))
    if any(t['score']['score'] is not None for t in topics):
        st.plotly_chart(score_breakdown(topics, demand_scoring.LABELS), key=f"predict-chart-{run['id']}", width='stretch',
                        theme=None, config={'displaylogo': False, 'displayModeBar': False})
        with st.expander('评分明细表'):
            st.dataframe([{'话题': t['title'], '类型': TOPIC_KINDS[t['kind']], '预测评分': t['score']['score'],
                           **{demand_scoring.LABELS[k]: round(v, 3) for k, v in score_contributions(t['score']).items()},
                           '提示': '；'.join(t['score']['flags'])} for t in topics], hide_index=True, width='stretch')
    if editing:
        impact(run, 1)
    titles = {t['id']: t['title'] for t in topics}
    with st.form(f"predict-topics-{run['id']}-{run['revision']}"):
        st.caption(f'勾选 1–{service.MAX_TOPICS} 个话题，作为候选方向的依据。')
        picked = []
        for topic in topics:
            score = topic['score']
            value = '未评分' if score['score'] is None else f"{score['score']:.2f}"
            date = f" · {topic['date'][:10]}" if topic['kind'] != 'search' and topic.get('date') else ''
            if st.checkbox(f"{topic['title']}（{value}）", value=topic['selected'], key=f"predict-topic-{run['id']}-{topic['id']}"):
                picked.append(topic['id'])
            st.caption(TOPIC_KINDS[topic['kind']] + date + ''.join(' · ' + f for f in score['flags']) + ' · ' + topic_note(topic))
        if not topics:
            st.caption('暂无话题。可以在下方「更多」里搜索近期热点或添加手动热点；未来 120 天内的节日节点会自动出现。')
        count = st.selectbox('候选数量', [3, 5], key=f"predict-count-{run['id']}")
        submit = st.form_submit_button('确认选题并生成候选', type='primary', disabled=busy or not topics)
    if submit:
        changed = set(picked) != set(run['topic_ids'])
        if changed and not action(engine, service.set_topics, run['id'], picked)[0]:
            return
        has_round = not changed and current_round(run) is not None
        st.session_state.pop(edit_key(run, 1), None)
        if has_round:
            st.toast('选题没有变化，保留当前候选。', icon=':material/info:')
        elif configured:
            flash(f'已确认 {len(picked)} 个选题：' + '、'.join(titles[i] for i in picked) + '。正在生成候选方向。')
            launch(engine, 'demand_candidates', run['id'], count=count)
        else:
            flash('已确认选题。配置文字模型后可生成候选方向。')
            st.rerun()
    with st.expander('更多：搜索近期热点、添加手动热点、评分权重'):
        st.caption('搜索近期热点会联网搜索并调用模型，按次计费；结果按日期核验，热度仍未验证。')
        if st.button('搜索近期热点', icon=':material/travel_explore:', disabled=busy or not configured, key=f"predict-search-{run['id']}"):
            launch(engine, 'demand_search', run['id'])
        step_feedback(engine, run['id'], last_op(run), ('demand_search',), 'flow-search')
        research = next((r for r in run['research'] if r['id'] == run['research_id']), None)
        if research:
            with st.expander('搜索话题的来源与日期依据'):
                for topic in research.get('topics', []):
                    st.write('**' + topic['title'] + '**')
                    for sid in topic['source_ids']:
                        st.caption(f"[{sid}] {research['extractions'][sid]['date']['reason']}")
                    evidence_view({s['id']: s for s in research['sources'] if s['id'] in topic['source_ids']})
                for issue in research['issues']:
                    st.caption(issue)
        st.markdown('**添加手动热点**')
        manual_topic_form(engine, run, f"predict-{run['id']}-", busy)
        st.markdown('**评分权重与人工热度**')
        scoring_form(engine, run, topics, f"predict-{run['id']}-", busy)


def topic_lines(run, topics):
    by_id = {t['id']: t for t in topics}
    lines = []
    for topic_id in run['topic_ids']:
        topic = by_id.get(topic_id)
        if topic:
            score = topic['score']['score']
            lines.append(f"{topic['title']}（{TOPIC_KINDS[topic['kind']]}" + (f' · 预测评分 {score:.2f}' if score is not None else '') + '）')
    return lines or ['已选话题不在当前列表中，请重新选题。']


# ③ 候选方向

def auto_key(run):
    return f"predict-auto-{run['id']}"


def start_pipeline(engine, run):
    st.session_state.pop(edit_key(run, 2), None)
    if st.session_state.get(auto_key(run)):
        flash('已确定方向，正在自动生成方案包、设计方案和纹样初稿。')
        launch(engine, 'demand_pipeline', run['id'])
    else:
        flash('已确定方向。下一步：生成方案包。')


def direction_view(engine, run, busy, editing):
    work = engine.workbench
    batch = next((b for b in reversed(run['candidate_rounds']) if not stale(run, b)), None)
    op = last_op(run)
    step_feedback(engine, run['id'], op, ('demand_candidates',), 'flow-candidates')
    if op and op['kind'] == 'demand_candidates' and op['status'] == 'running':
        return
    if editing:
        impact(run, 2)
    if batch is None:
        st.caption('当前选题还没有候选方向。请在下方「更多」里生成，或回到第 ② 步重新确认选题。')
    else:
        round_topics_caption(batch)
        ready = work.llm.configured and work.images.configured
        st.checkbox('确定方向后自动完成方案包、设计方案和纹样初稿', key=auto_key(run), disabled=busy or not ready,
                    help='默认不勾选。勾选后选定方向会依次生成第 ④、⑤ 步的结果，需要配置文字模型和图片服务。')
        if not work.images.configured:
            st.caption('自动完成需要先在系统设置中配置图片服务。')
        candidate_cards(engine, run, batch, busy, after_select=lambda: start_pipeline(engine, run))
        if batch.get('capacity_log'):
            with st.expander(f"产能检查记录 · {len(batch['capacity_log'])} 个方向已自动修正"):
                for item in batch['capacity_log']:
                    st.write(item['name'] + '：' + '；'.join(item['problems']))
    with st.expander('更多：重新生成候选、导出本轮对比、参考资料'):
        key = f"predict-regen-{run['id']}-"
        with st.form(key + 'form'):
            a, b = st.columns([1, 3], vertical_alignment='bottom')
            count = a.selectbox('候选数量', [3, 5], key=key + 'count')
            instruction = b.text_input('调整意见（选填）', max_chars=4000, key=key + 'instruction')
            regen = st.form_submit_button('重新生成候选', disabled=busy or not work.llm.configured)
        if regen:
            launch(engine, 'demand_candidates', run['id'], count=count, instruction=instruction)
        if batch:
            st.download_button('导出本轮对比（Markdown）', round_markdown(run, batch), file_name=f"candidates-{run['id'][:8]}.md",
                               mime='text/markdown', icon=':material/download:', key=key + 'export')
            evidence_view(batch['context']['evidence'])


# ④ 方案包

def package_view(engine, run, brief, busy, editing, advance_key=None):
    work = engine.workbench
    op = last_op(run)
    step_feedback(engine, run['id'], op, ('demand_brief', 'demand_revise', 'demand_pipeline'), 'flow-package')
    if op and op['kind'] in ('demand_brief', 'demand_revise', 'demand_pipeline') and op['status'] == 'running':
        return
    if brief is None:
        st.caption('已选方向：' + '；'.join(direction_lines(run)))
        if st.button('生成方案包', type='primary', icon=':material/description:', disabled=busy or not work.llm.configured,
                     key=f"predict-brief-generate-{run['id']}"):
            launch(engine, 'demand_brief', run['id'])
        return
    brief_content(run, brief)
    if advance_key and st.button('下一步：设计与纹样', type='primary', icon=':material/arrow_forward:', key=advance_key + '-button'):
        st.session_state[advance_key] = True
        st.rerun()
    flow.brief_more(engine, run, brief, busy, editing, PREFIX, edit_key(run, 3))


# ⑤ 设计与纹样

# Page

def predict_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('爆款产品研判工作平台', '按「需求与产能 → 选题 → 候选方向 → 方案包 → 设计与纹样」五步推进，每一步确认后再进入下一步。', 'HOT PRODUCT PREDICTION')
    flow.show_flash(PREFIX)
    a, b = st.columns([4, 1], vertical_alignment='bottom')
    with a:
        run = task_select(engine, 'trend', '研判任务')
    if b.button('新建研判任务', icon=':material/add:', disabled=busy, width='stretch'):
        st.session_state['predict-new-open'] = True
    # Kept open through session state so widget changes inside the dialog do not close it.
    if st.session_state.get('predict-new-open') and not busy:
        new_task_dialog(engine)
    if not work.llm.configured:
        st.info('可以先建任务和选题；分析需求和生成候选前，请在「系统 → 模型、企业与日历设置」中配置文字模型。')
    if run is None:
        st.info('还没有研判任务。点击「新建研判任务」，选择企业档案并填写需求。')
        return
    settle_analysis(engine, run, busy)
    brief = current_brief(run)
    advance_key = f"predict-advance-{brief['id']}" if brief else None
    done, current, brief, design = flow_state(run, bool(advance_key and st.session_state.get(advance_key)))
    op = last_op(run)
    auto = bool(op and op['kind'] == 'demand_pipeline' and op['status'] == 'running')
    states = []
    for i in range(len(STEPS)):
        if done[i]:
            states.append('done')
        elif auto and i >= 3:
            # The pipeline fills the package and design steps in turn.
            states.append('auto')
        else:
            states.append('current' if i == current else 'locked')
    flow.flow_bar(STEPS, states, '研判流程')
    requested = st.query_params.get('step')
    topics = work.demands.topics(run)
    summaries = {0: lambda: [run['inputs']['request'], *requirement_lines(run['inputs'])[:2]],
                 1: lambda: topic_lines(run, topics), 2: lambda: direction_lines(run), 3: lambda: flow.brief_lines(brief)}
    for i, name in enumerate(STEPS):
        with st.container(border=True, key=f'flow-step-{i}'):
            if states[i] == 'locked':
                flow.step_title(STEPS, i, 'locked', f'需先完成第 {NUMBERS[i - 1]} 步')
                continue
            editing = done[i] and st.session_state.get(edit_key(run, i), False)
            expanded = states[i] in ('current', 'auto') or editing or i == 4 or requested == name
            title, edit = st.columns([6, 1], vertical_alignment='center')
            with title:
                flow.step_title(STEPS, i, states[i])
            if done[i] and i < 4:
                edit.button('取消修改' if editing else '修改', key=edit_key(run, i) + '-button', on_click=flow.toggle, args=(edit_key(run, i),),
                            disabled=busy, width='stretch')
            if not expanded:
                for line in summaries[i]():
                    st.caption(line)
                continue
            if i == 0 and done[0]:
                need_edit(engine, run, busy)
            elif i == 0:
                need_current(engine, run, busy)
            elif i == 1:
                topic_view(engine, run, topics, busy, editing)
            elif i == 2:
                direction_view(engine, run, busy, editing)
            elif i == 3:
                package_view(engine, run, brief, busy, editing, None if done[3] else advance_key)
            else:
                flow.design_view(engine, run, brief, design, busy, PREFIX)
    flow.version_record(run)
    boundary(['预测评分由时效、节点临近、能力关联与人工热度按权重合成，不是真实热度或销量预测；权重在第 ② 步「更多」中调整。',
              '爆款潜力为模型判断；产能检查只核对品类、工艺、交期和数量，未设置产能时不过滤。',
              '日历节点与手动热点用于安排上市时间和选题，不能作为事实引用；搜索话题的日期核验不代表热度已验证。',
              '修改已完成的步骤会让之后的结果变为历史版本，提交前会列出受影响的内容；历史版本在「版本记录」中保留。'])
