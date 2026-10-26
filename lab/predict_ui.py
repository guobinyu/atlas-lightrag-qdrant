"""Hotspot prediction workbench: one-way five steps, one step open at a time, one primary action per step."""
from copy import deepcopy
from html import escape
import json

import streamlit as st

from lab import demand_scoring
from lab.charts import score_breakdown, score_contributions
from lab.demand_ui import (TOPIC_KINDS, action, candidate_cards, capacity_form, confirmation_view, design_cards,
                           element_widgets, enterprise_picker, evidence_view, history_view, image_step, inline_questions,
                           launch, manual_topic_form, round_topics_caption, scoring_form, step_feedback, task_select)
from lab.demands import (brief_sections, can_answer, current_brief, current_design, export_bundle, export_markdown,
                         round_markdown, stale, structured_lines, topic_note)
from lab.enterprises import Enterprises
from lab.ui_kit import badges, boundary, category_icon

STEPS = ['需求与产能', '选题', '候选方向', '方案包', '设计与纹样']
NUMBERS = '①②③④⑤'
STATE_LABELS = {'done': '已完成', 'current': '进行中', 'auto': '自动生成中', 'locked': '待完成'}
AWAIT = 'predict-await'
FLASH = 'predict-flash'
DESIGN_KINDS = ('demand_design_generate', 'demand_design_revise', 'demand_image_generate', 'demand_image_revise')
IMAGE_MODES = {'pattern': '纹样初稿（可平铺单元）', 'application': '应用效果图', 'none': '仅文字方案'}


def last_op(run):
    ops = list(run['operations'].values())
    return ops[-1] if ops else None


def latest_analysis(run):
    return next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)


def current_round(run):
    if run['selection']:
        return next((b for b in run['candidate_rounds'] if b['id'] == run['selection']['round_id']), None)
    return next((b for b in reversed(run['candidate_rounds']) if not stale(run, b)), None)


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


def flow_bar(states):
    items = ''.join(f'<li class="flow-step {state}"' + (' aria-current="step"' if state == 'current' else '') + '>'
                    f'<b>{NUMBERS[i]}</b><span>{STEPS[i]}</span><small>{STATE_LABELS[state]}</small></li>'
                    for i, state in enumerate(states))
    st.markdown(f'<ol class="flow-bar" aria-label="研判流程">{items}</ol>', unsafe_allow_html=True)


def step_title(i, state, note=''):
    st.markdown(f'<div class="flow-title {state}"><b>{NUMBERS[i]}</b> {STEPS[i]}'
                f'<small>{STATE_LABELS[state]}{" · " + escape(note) if note else ""}</small></div>', unsafe_allow_html=True)


def edit_key(run, i):
    return f"predict-edit-{i}-{run['id']}"


def toggle(key):
    st.session_state[key] = not st.session_state.get(key, False)


def flash(message):
    # A toast raised right before st.rerun() is lost, so show it on the next run.
    st.session_state[FLASH] = message


def impact(run, step):
    """Results that become history when the given step is changed; listed before anything is submitted."""
    brief = current_brief(run)
    rounds = [b for b in run['candidate_rounds'] if not stale(run, b)]
    designs = [v for v in run.get('design_versions', []) if brief and v['brief_id'] == brief['id']]
    images = [a for a in run.get('image_assets', []) if any(a['design_id'] == v['id'] for v in designs)]
    items = []
    if step <= 1 and rounds:
        items.append(f'{len(rounds)} 轮候选')
    if step <= 2 and run['selection']:
        items.append('已选方向')
    if step <= 3 and brief:
        items.append(f"方案包（版本 {brief['version']}）")
    if designs:
        items.append(f'{len(designs)} 版设计方案' + (f'及 {len(images)} 张图稿' if images else ''))
    if items:
        st.warning('提交后以下结果会变为历史版本，仍可在页面底部「版本记录」中查看：' + '、'.join(items))
    else:
        st.caption('目前没有后续结果，修改不会影响其他内容。')


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


def direction_lines(run):
    batch = current_round(run)
    candidate = next((c for c in batch['candidates'] if c['id'] == run['selection']['candidate_id']), None) if batch else None
    if not candidate:
        return ['已选方向不可用，请重新选择。']
    lines = [f"{category_icon(candidate.get('requirements', {}).get('category', candidate['form']))} {candidate['name']}"]
    if candidate.get('requirements'):
        req = candidate['requirements']
        lines.append(req['category'] + (' · ' + '、'.join(req['crafts']) if req['crafts'] else '')
                     + f" · 爆款潜力 {candidate['potential']['score']}/5（模型判断）")
    return lines


# ④ 方案包

def brief_content(run, brief):
    sections = brief_sections(run, brief)
    st.markdown('#### ' + escape(sections[0][0]))
    st.caption(sections[0][1])
    for title, body, auxiliary in sections[1:]:
        if not auxiliary and title in ('推荐产品方案', '产品方案包与需求规格书'):
            st.markdown('**' + title + '**')
            st.markdown(body)
        else:
            with st.expander(title):
                st.markdown(body)


def brief_more(engine, run, brief, busy, editing):
    with st.expander('修订方案包与导出', expanded=editing):
        if editing:
            impact(run, 3)
        key = f"predict-brief-{brief['id']}-"
        with st.form(key + 'revise'):
            instruction = st.text_area('修订意见', max_chars=4000)
            changes = element_widgets({k: v for k, v in brief['evidence'].items() if 'element_id' in v}, key, busy)
            revise = st.form_submit_button('生成方案包新版本', disabled=busy or not engine.workbench.llm.configured)
        if revise:
            st.session_state.pop(edit_key(run, 3), None)
            launch(engine, 'demand_revise', run['id'], brief_id=brief['id'], instruction=instruction, element_changes=changes)
        a, b = st.columns(2)
        a.download_button('下载 Markdown', export_markdown(run, brief), file_name=f"package-v{brief['version']}.md", mime='text/markdown', key=key + 'md')
        b.download_button('下载 JSON', json.dumps(export_bundle(run, brief), ensure_ascii=False, indent=2),
                          file_name=f"package-v{brief['version']}.json", mime='application/json', key=key + 'json')
        evidence_view(brief['evidence'])


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
    brief_more(engine, run, brief, busy, editing)


def package_lines(brief):
    package = brief['content'].get('package') or {}
    lines = [f"{brief['content']['title']}（版本 {brief['version']}）"]
    if package.get('pricing'):
        lines.append(f"定价参考 {package['pricing']['low']}–{package['pricing']['high']} 元／件 · 风险 {len(package.get('risks', []))} 项")
    return lines


# ⑤ 设计与纹样

def generate_options(engine, run, brief, busy, label):
    work = engine.workbench
    key = f"predict-design-{run['id']}-{brief['id']}-"
    with st.expander('生成选项', expanded=False):
        variants = st.selectbox('一次生成方案数', [1, 3], format_func=lambda n: '1 版' if n == 1 else '3 版对比（仅文字，选定一版后再出图）',
                                key=key + 'variants', disabled=busy)
        mode = st.radio('图稿', list(IMAGE_MODES), format_func=IMAGE_MODES.get, key=key + 'mode', horizontal=True,
                        disabled=busy or variants == 3 or not work.images.configured)
    if not work.images.configured:
        st.caption('尚未配置图片服务，本次只生成文字方案。')
    with_images = work.images.configured and variants == 1 and mode != 'none'
    if st.button(label, type='primary', icon=':material/auto_awesome:', disabled=busy or not work.llm.configured, key=key + 'generate'):
        inputs = {'brief_id': brief['id'], 'variants': variants, 'include_images': with_images}
        if with_images:
            inputs['image_mode'] = mode
        launch(engine, 'demand_design_generate', run['id'], **inputs)


def design_view(engine, run, brief, design, busy):
    from lab import demand_design
    from lab.design_pdf import export_pdf
    work = engine.workbench
    op = last_op(run)
    step_feedback(engine, run['id'], op, ('demand_design_generate', 'demand_design_revise', 'demand_pipeline'), 'flow-design')
    if op and op['kind'] in ('demand_design_generate', 'demand_design_revise', 'demand_pipeline') and op['status'] == 'running':
        return
    if design is None:
        versions = [v for v in run.get('design_versions', []) if v['brief_id'] == brief['id'] and not stale(run, v)]
        if versions and versions[-1].get('group_id'):
            design_cards(engine, run, brief, busy, prefix='predict', heading='选择一版设计方案')
            st.caption('确认一版后，在这一步继续生成纹样初稿或效果图。')
        else:
            st.caption('方案包：' + '；'.join(package_lines(brief)))
            generate_options(engine, run, brief, busy, '生成设计与纹样' if work.images.configured else '生成设计方案')
        return
    content = demand_design.sections(run, design)
    st.markdown('#### ' + escape(content[0][0]))
    st.caption(content[0][1])
    for tab, (_, body, _) in zip(st.tabs(['整体概念', '视觉设计', '产品应用与交付']), content[1:4]):
        with tab:
            st.markdown(body)
    for title, body, _ in content[4:]:
        with st.expander(title):
            st.markdown(body)
    image_step(engine, run, design, True, op)
    with st.expander('修订、导出与重新生成'):
        key = f"predict-design-{design['id']}-"
        with st.form(key + 'revise'):
            instruction = st.text_area('设计方案修改意见', placeholder='例如：主色改为墨绿，保留故事，简化构图。', max_chars=4000)
            changes = element_widgets({k: v for k, v in brief['evidence'].items() if 'element_id' in v}, key + 'elements', busy)
            revise = st.form_submit_button('生成设计方案新版本', disabled=busy or not work.llm.configured)
        if revise:
            saved = {a.get('mode', 'application') for a in run.get('image_assets', []) if a['design_id'] == design['id']}
            launch(engine, 'demand_design_revise', run['id'], design_id=design['id'], instruction=instruction, element_changes=changes,
                   include_images=work.images.configured, image_mode='pattern' if 'pattern' in saved or not saved else 'application')
        pdf_run, pdf_design = deepcopy(run), deepcopy(design)
        a, b, c = st.columns(3)
        a.download_button('导出 PDF', lambda: export_pdf(work.demands, pdf_run, pdf_design), file_name=f"design-{run['id'][:8]}-v{design['version']}.pdf",
                          mime='application/pdf', key=key + 'pdf', on_click='ignore')
        b.download_button('下载 Markdown', demand_design.export_markdown(run, design), file_name=f"design-{run['id'][:8]}-v{design['version']}.md",
                          mime='text/markdown', key=key + 'md')
        c.download_button('下载 JSON', json.dumps(demand_design.export_bundle(run, design), ensure_ascii=False, indent=2),
                          file_name=f"design-{run['id'][:8]}-v{design['version']}.json", mime='application/json', key=key + 'json')
        st.divider()
        generate_options(engine, run, brief, busy, '重新生成设计方案')


# Page

def version_record(run):
    from lab import demand_design
    from lab.campaign_ui import local_time
    with st.expander('版本记录', icon=':material/history:'):
        rounds, briefs, designs, log = st.tabs(['候选轮次', '方案包', '设计方案', '执行记录'])
        with rounds:
            for batch in reversed(run['candidate_rounds']):
                st.markdown(f"{local_time(batch['at'])} · {'历史' if stale(run, batch) else '当前'} · "
                            + '、'.join(escape(c['name']) for c in batch['candidates']))
                st.download_button('导出', round_markdown(run, batch), file_name=f"candidates-{batch['id'][:8]}.md", mime='text/markdown',
                                   key='record-round-' + batch['id'])
            if not run['candidate_rounds']:
                st.caption('暂无。')
        with briefs:
            for brief in reversed(run['briefs']):
                historical = stale(run, brief) or brief['selection'] != run['selection']
                st.markdown(f"版本 {brief['version']} · {escape(brief['content']['title'])} · {'历史' if historical else '当前'}")
                st.download_button('导出', export_markdown(run, brief), file_name=f"package-v{brief['version']}.md", mime='text/markdown',
                                   key='record-brief-' + brief['id'])
            if not run['briefs']:
                st.caption('暂无。')
        with designs:
            for design in reversed(run.get('design_versions', [])):
                historical = stale(run, design) or design['selection'] != run['selection']
                st.markdown(f"版本 {design['version']} · {escape(design['content']['title'])} · {'历史' if historical else '当前'}")
                st.download_button('导出', demand_design.export_markdown(run, design), file_name=f"design-v{design['version']}.md",
                                   mime='text/markdown', key='record-design-' + design['id'])
            if not run.get('design_versions'):
                st.caption('暂无。')
        with log:
            history_view(run)


def predict_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('爆款产品研判工作平台', '按「需求与产能 → 选题 → 候选方向 → 方案包 → 设计与纹样」五步推进，每一步确认后再进入下一步。', 'HOT PRODUCT PREDICTION')
    message = st.session_state.pop(FLASH, None)
    if message:
        st.toast(message, icon=':material/check_circle:')
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
    flow_bar(states)
    requested = st.query_params.get('step')
    topics = work.demands.topics(run)
    summaries = {0: lambda: [run['inputs']['request'], *requirement_lines(run['inputs'])[:2]],
                 1: lambda: topic_lines(run, topics), 2: lambda: direction_lines(run), 3: lambda: package_lines(brief)}
    for i, name in enumerate(STEPS):
        with st.container(border=True, key=f'flow-step-{i}'):
            if states[i] == 'locked':
                step_title(i, 'locked', f'需先完成第 {NUMBERS[i - 1]} 步')
                continue
            editing = done[i] and st.session_state.get(edit_key(run, i), False)
            expanded = states[i] in ('current', 'auto') or editing or i == 4 or requested == name
            title, edit = st.columns([6, 1], vertical_alignment='center')
            with title:
                step_title(i, states[i])
            if done[i] and i < 4:
                edit.button('取消修改' if editing else '修改', key=edit_key(run, i) + '-button', on_click=toggle, args=(edit_key(run, i),),
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
                design_view(engine, run, brief, design, busy)
    version_record(run)
    boundary(['预测评分由时效、节点临近、能力关联与人工热度按权重合成，不是真实热度或销量预测；权重在第 ② 步「更多」中调整。',
              '爆款潜力为模型判断；产能检查只核对品类、工艺、交期和数量，未设置产能时不过滤。',
              '日历节点与手动热点用于安排上市时间和选题，不能作为事实引用；搜索话题的日期核验不代表热度已验证。',
              '修改已完成的步骤会让之后的结果变为历史版本，提交前会列出受影响的内容；历史版本在「版本记录」中保留。'])
