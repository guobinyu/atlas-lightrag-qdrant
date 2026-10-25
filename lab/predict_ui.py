"""Hotspot prediction workbench (proposal sketch 1): topic board, enterprise capacity, ranked candidate cards."""
import streamlit as st

from lab import demand_scoring
from lab.charts import score_breakdown, score_contributions
from lab.demand_ui import (TOPIC_KINDS, action, analyze_dialog, brief_step, candidate_cards, capacity_form, design_step,
                           dialog_host, enterprise_picker, evidence_view, history_view, launch, manual_topic_form,
                           open_dialog, round_topics_caption, scoring_form, step_feedback, task_select)
from lab.demands import can_answer, round_markdown, stale, structured_lines, topic_note
from lab.enterprises import Enterprises
from lab.ui_kit import badges, boundary, goto_step, panel_title, step_tabs

STEPS = ['研判', '方案包', '设计与纹样']


def apply_inputs(engine, run, changes):
    """Merchant edits create a new requirement version; prediction tasks stay confirmed for the new version."""
    service = engine.workbench.demands
    ok, _ = action(engine, service.update, run['id'], {**run['inputs'], **changes}, run['summaries'][-1]['answers'])
    if ok:
        ok, _ = action(engine, service.approve, run['id'])
    if ok:
        st.rerun()


def close_new_task():
    st.session_state.pop('predict-new-open', None)


@st.dialog('新建研判任务', width='large', on_dismiss=close_new_task)
def new_task_dialog(engine):
    service = engine.workbench.demands
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
    if st.button('创建并开始研判', type='primary', key=key + 'create'):
        ok, run = action(engine, service.create, {'request': request, 'industry': industry, 'brand': brand, 'capabilities': capabilities,
                                                  'constraints': constraints, 'capacity': capacity, 'budget': budget, 'path': 'trend', 'days': days})
        if ok:
            close_new_task()
            st.query_params['demand'] = run['id']
            st.session_state['trend-task'] = run['id']
            if engine.workbench.llm.configured:
                # The model reads the requirement first; questions open the clarification dialog when it finishes.
                analyze_dialog(engine, run['id'])
            elif action(engine, service.approve, run['id'])[0]:
                st.rerun()


def topic_board(engine, run, busy):
    service = engine.workbench.demands
    configured = engine.workbench.llm.configured
    approved = run['approved_revision'] == run['revision']
    topics = service.topics(run)
    panel_title('热点监测看板')
    st.markdown(badges('score', 'heat'))
    if any(t['score']['score'] is not None for t in topics):
        st.plotly_chart(score_breakdown(topics, demand_scoring.LABELS), key=f"predict-chart-{run['id']}", width='stretch',
                        theme=None, config={'displaylogo': False, 'displayModeBar': False})
        with st.expander('评分明细表'):
            st.dataframe([{'话题': t['title'], '类型': TOPIC_KINDS[t['kind']], '预测评分': t['score']['score'],
                           **{demand_scoring.LABELS[k]: round(v, 3) for k, v in score_contributions(t['score']).items()},
                           '提示': '；'.join(t['score']['flags'])} for t in topics], hide_index=True, width='stretch')
    flash = st.session_state.pop('predict-topic-flash', None)
    if flash:
        st.toast(flash, icon=':material/check_circle:')
    titles = {t['id']: t['title'] for t in topics}
    if run['topic_ids']:
        st.success('当前选题：' + '、'.join(titles.get(i, i) for i in run['topic_ids']), icon=':material/check_circle:')
    else:
        st.info('尚未确认选题。勾选话题后点「确认选题」。')
    with st.form(f"predict-topics-{run['id']}-{run['revision']}"):
        st.caption(f'勾选 1–{service.MAX_TOPICS} 个话题，作为本轮候选的依据。')
        picked = []
        for topic in topics:
            score = topic['score']
            value = '未评分' if score['score'] is None else f"{score['score']:.2f}"
            date = f" · {topic['date'][:10]}" if topic['kind'] != 'search' and topic.get('date') else ''
            if st.checkbox(f"{topic['title']}（{value}）", value=topic['selected'], key=f"predict-topic-{run['id']}-{topic['id']}"):
                picked.append(topic['id'])
            st.caption(TOPIC_KINDS[topic['kind']] + date + ''.join(' · ' + f for f in score['flags']) + ' · ' + topic_note(topic))
        if not topics:
            st.caption('暂无话题。可以搜索近期热点或添加手动热点；未来 120 天内的节日节点会自动出现。')
        save = st.form_submit_button('确认选题', type='primary', disabled=busy or not topics)
    if save:
        if picked and set(picked) == set(run['topic_ids']):
            st.toast('选题没有变化。', icon=':material/info:')
        else:
            ok, _ = action(engine, service.set_topics, run['id'], picked)
            if ok:
                # A toast called before rerun is lost, so show it on the next run.
                message = f'已确认 {len(picked)} 个选题：' + '、'.join(titles[i] for i in picked)
                if run['selection']:
                    message += '。原已选方向已清空，请重新生成备选方案。'
                st.session_state['predict-topic-flash'] = message
                st.rerun()
    if st.button('搜索近期热点', icon=':material/travel_explore:', disabled=busy or not configured or not approved, key=f"predict-search-{run['id']}"):
        launch(engine, 'demand_search', run['id'])
    step_feedback(engine, run['id'], last_op(run), ('demand_search',), 'predict-search')
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
    with st.expander('添加手动热点'):
        manual_topic_form(engine, run, f"predict-{run['id']}-", busy)
    with st.expander('评分权重与人工热度'):
        scoring_form(engine, run, topics, f"predict-{run['id']}-", busy)


def capacity_panel(engine, run, busy):
    inputs = run['inputs']
    panel_title('企业产能')
    profile_id, fields = enterprise_picker(engine, f"predict-profile-{run['id']}", busy, '选择企业')
    if profile_id and st.button('应用到当前任务', key=f"predict-apply-{run['id']}", disabled=busy, width='stretch'):
        apply_inputs(engine, run, fields)
    with st.container(border=True):
        st.markdown(f"**{inputs['industry']}**" + (f" · {inputs['brand']}" if inputs.get('brand') else ''))
        st.caption(inputs['capabilities'])
        for line in structured_lines(inputs):
            st.write(line)
        if not inputs.get('capacity'):
            st.caption('未设置产能参数，候选不做产能硬性检查。')
        if inputs.get('constraints'):
            st.caption('限制：' + inputs['constraints'])
    with st.popover('编辑产能与预算', icon=':material/tune:', width='stretch', disabled=busy):
        capacity, budget = capacity_form(inputs, f"predict-cap-{run['id']}-{run['revision']}-", busy, wrap=False)
        st.caption('保存后生成新的需求版本，已有候选标为历史条件。')
        if st.button('保存', type='primary', key=f"predict-cap-save-{run['id']}"):
            apply_inputs(engine, run, {'capacity': capacity, 'budget': budget})
    with st.popover('另存为企业档案', icon=':material/save:', width='stretch'):
        name = st.text_input('档案名称', max_chars=60, key=f"predict-profile-name-{run['id']}")
        if st.button('保存档案', key=f"predict-profile-save-{run['id']}"):
            ok, _ = action(engine, engine.workbench.enterprises.save, Enterprises.from_task(name, inputs))
            if ok:
                st.success('已保存，可在新建任务时选择。')


def candidate_panel(engine, run, busy):
    panel_title('爆款候选列表')
    if not run['candidate_rounds']:
        st.caption('确认选题后，点击下方「生成备选方案」。')
        return None
    batches = {b['id']: b for b in reversed(run['candidate_rounds'])}
    from lab.campaign_ui import local_time
    bid = st.selectbox('候选轮次', list(batches), key=f"predict-round-{run['id']}-{len(batches)}",
                       format_func=lambda k: f"{local_time(batches[k]['at'])} · {k[:6]}" + (' · 历史条件' if stale(run, batches[k]) else ''))
    batch = batches[bid]
    if stale(run, batch):
        st.markdown(badges('history'))
        st.caption('这轮候选对应历史条件，可以查看，不能用于当前方案。')
    round_topics_caption(batch)
    if batch.get('capacity_log'):
        with st.expander(f"产能检查记录 · {len(batch['capacity_log'])} 个方向已自动修正"):
            for item in batch['capacity_log']:
                st.write(item['name'] + '：' + '；'.join(item['problems']))
    candidate_cards(engine, run, batch, busy)
    with st.expander('本轮参考资料'):
        evidence_view(batch['context']['evidence'])
    return batch


def action_bar(engine, run, batch, busy):
    work = engine.workbench
    configured, approved = work.llm.configured, run['approved_revision'] == run['revision']
    key = f"predict-actions-{run['id']}-"
    with st.container(border=True, key='predict-actions'):
        a, b, c, d, f, e = st.columns([0.8, 1, 1.1, 1, 1.1, 1.1], vertical_alignment='bottom')
        count = a.selectbox('候选数量', [3, 5], key=key + 'count')
        with b.popover('本轮调整意见', icon=':material/edit_note:', width='stretch'):
            st.text_area('调整意见（选填）', max_chars=4000, key=key + 'instruction')
        if c.button('生成备选方案', type='primary', icon=':material/auto_awesome:', key=key + 'generate', width='stretch',
                    disabled=busy or not configured or not approved or not run['topic_ids']):
            launch(engine, 'demand_candidates', run['id'], count=count, instruction=st.session_state.get(key + 'instruction', ''))
        d.download_button('导出方案', round_markdown(run, batch) if batch else '', file_name=f"candidates-{run['id'][:8]}.md",
                          mime='text/markdown', icon=':material/download:', disabled=batch is None, key=key + 'export', width='stretch')
        current = bool(run['selection']) and approved
        if f.button('生成方案包', icon=':material/description:', key=key + 'brief', width='stretch',
                    disabled=busy or not configured or not current,
                    help='为已选方向生成需求单、产品方案包与需求规格书，完成后在「方案包」页查看和修订。'):
            goto_step('predict-steps', '方案包')
            launch(engine, 'demand_brief', run['id'])
        if e.button('下发纹样任务', icon=':material/texture:', key=key + 'pipeline', width='stretch',
                    disabled=busy or not configured or not work.images.configured or not current,
                    help='依次生成方案包、一版设计方案和三张可平铺纹样初稿。需要已选方向并配置图片服务。'):
            launch(engine, 'demand_pipeline', run['id'])
        if not run['topic_ids']:
            st.caption('请先在热点监测看板确认选题。')
        elif not run['selection']:
            st.caption('生成备选方案后，在候选卡片上点「选为方向」，再生成方案包或下发纹样任务。')
        else:
            st.caption('已选方向。下一步：「生成方案包」逐步推进，或「下发纹样任务」一次生成方案包、设计方案和纹样初稿。'
                       + ('' if work.images.configured else ' 下发纹样任务需要先在系统设置中配置图片服务。'))
    op = last_op(run)
    step_feedback(engine, run['id'], op, ('demand_candidates',), 'predict-candidates')
    step_feedback(engine, run['id'], op, ('demand_pipeline',), 'predict-pipeline')
    step_feedback(engine, run['id'], op, ('demand_brief',), 'predict-brief')


def last_op(run):
    ops = list(run['operations'].values())
    return ops[-1] if ops else None


def predict_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('爆款产品研判工作平台', '结合节日节点、近期热点与企业产能，研判值得批量开发的丝绸细分产品，并一键下发纹样任务。', 'HOT PRODUCT PREDICTION')
    a, b = st.columns([4, 1], vertical_alignment='bottom')
    with a:
        run = task_select(engine, 'trend', '研判任务')
    if b.button('新建研判任务', icon=':material/add:', disabled=busy, width='stretch'):
        st.session_state['predict-new-open'] = True
    # Kept open through session state so widget changes inside the dialog do not close it.
    if st.session_state.get('predict-new-open') and not busy:
        new_task_dialog(engine)
    if not work.llm.configured:
        st.info('可以先建任务和选题；生成候选前请在「系统 → 模型、企业与日历设置」中配置文字模型。')
    if run is None:
        st.info('还没有研判任务。点击「新建研判任务」，选择企业档案并填写需求。')
        return
    dialog_host(engine, run, busy, auto_approve=True)
    analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
    approved = run['approved_revision'] == run['revision']
    with st.container(horizontal=True, vertical_alignment='center'):
        st.markdown(f"**需求**：{run['inputs']['request']}")
        if not approved and st.button('确认当前需求', type='primary', disabled=busy, key=f"predict-approve-{run['id']}"):
            if action(engine, work.demands.approve, run['id'])[0]:
                st.rerun()
        if st.button('完善需求', disabled=busy or not work.llm.configured, key=f"predict-refine-{run['id']}"):
            if analysis:
                open_dialog(run['id'], 'questions' if can_answer(run, analysis) else 'confirm')
            else:
                analyze_dialog(engine, run['id'])
    research_tab, package_tab, design_tab = step_tabs(STEPS, 'predict-steps')
    with research_tab:
        left, middle, right = st.columns([1.25, 0.9, 1.25], gap='medium')
        with left, st.container(border=True, key='predict-board'):
            topic_board(engine, run, busy)
        with middle, st.container(border=True, key='predict-capacity'):
            capacity_panel(engine, run, busy)
        with right, st.container(border=True, key='predict-candidates'):
            batch = candidate_panel(engine, run, busy)
        action_bar(engine, run, batch, busy)
    prefix = f"demand-{run['id']}-r{run['revision']}-"
    with package_tab:
        selected_brief = brief_step(engine, run, prefix, last_op(run), heading='方案包与需求规格书')
    with design_tab:
        design_step(engine, run, selected_brief, last_op(run), heading='设计方案与纹样')
    with st.expander('需求与执行历史'):
        history_view(run)
    boundary(['预测评分由时效、节点临近、能力关联与人工热度按权重合成，不是真实热度或销量预测；权重可在看板下方调整。',
              '爆款潜力为模型判断；产能检查只核对品类、工艺、交期和数量，未设置产能时不过滤。',
              '日历节点与手动热点用于安排上市时间和选题，不能作为事实引用；搜索话题的日期核验不代表热度已验证。'])
