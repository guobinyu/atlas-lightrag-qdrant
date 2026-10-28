"""Custom design workbench: one-way four steps for specific audiences and scenes. No hotspot search."""
from html import escape

import streamlit as st

from lab import flow_ui as flow
from lab.demand_presets import STYLES, TEMPLATES, TEMPLATE_FIELDS
from lab.demand_ui import (action, candidate_cards, capacity_form, confirmation_view, enterprise_picker, evidence_view,
                           launch, step_feedback, task_select)
from lab.demands import (FIELDS, MAX_CLARIFICATION_ROUNDS, can_answer, clarification_rounds, current_brief, current_design,
                         stale, structured_lines)
from lab.flow_ui import NUMBERS, brief_content, direction_lines, last_op, latest_analysis, running
from lab.ui_kit import boundary, page_enabled, panel_title, swatches

STEPS = ['需求对话', '产品方向', '文化元素与设计说明', '设计方案']
PREFIX = 'custom'
# Changing step i makes results from this point on historical.
IMPACT = ('rounds', 'selection', 'brief')
TEMPLATE_ICONS = {'wedding': 'celebration', 'uniform': 'school', 'alumni': 'diversity_3', 'souvenir': 'card_giftcard'}


def edit_key(run, i):
    return flow.edit_key(PREFIX, run, i)


def custom_state(run, advanced=False):
    """(done flags, index of the current step or None, current brief, current design). Pure; used by tests.
    advanced: the user moved past the design brief; any design for the current brief counts as well."""
    brief = current_brief(run)
    design = current_design(run, brief)
    designed = bool(brief) and any(v['brief_id'] == brief['id'] for v in run.get('design_versions', []))
    done = [run['approved_revision'] == run['revision'], bool(run['selection']), bool(brief) and (advanced or designed), bool(design)]
    for i in range(1, len(done)):
        done[i] = done[i] and done[i - 1]
    return done, next((i for i, d in enumerate(done) if not d), None), brief, design


# ① 需求对话

def pick_template(key):
    current = st.session_state.get('custom-template')
    st.session_state['custom-template'] = None if current == key else key


def pending_key(run):
    return f"custom-switch-pending-{run['id']}"


def switch_template(engine, run, key):
    """Existing task: record the template and prefill only empty fields; this creates a new requirement version."""
    inputs = {**run['inputs'], 'template': key}
    for field in TEMPLATE_FIELDS:
        inputs[field] = inputs.get(field) or TEMPLATES[key][field]
    ok, _ = action(engine, engine.workbench.demands.update, run['id'], inputs, run['summaries'][-1]['answers'])
    if ok:
        st.session_state.pop(pending_key(run), None)
        flow.flash(PREFIX, f"已切换为「{TEMPLATES[key]['label']}」模板，请重新确认需求。")
        st.rerun()


def confirm_switch(engine, run, busy):
    key = st.session_state.get(pending_key(run))
    if not key:
        return
    st.markdown(f"**切换为「{TEMPLATES[key]['label']}」模板？**")
    st.caption('只补充尚未填写的人群、场景、用途和主题；切换后需要重新确认需求。')
    flow.impact(run, 'rounds', '设计说明')
    a, b = st.columns(2)
    if a.button('确认切换', type='primary', key=f"custom-switch-confirm-{run['id']}", disabled=busy, width='stretch'):
        switch_template(engine, run, key)
    if b.button('取消', key=f"custom-switch-cancel-{run['id']}", width='stretch'):
        st.session_state.pop(pending_key(run), None)
        st.rerun()


def template_panel(engine, run, busy):
    panel_title('场景模板')
    current = run['inputs'].get('template') if run else st.session_state.get('custom-template')
    keys = list(TEMPLATES)
    for row in (keys[:2], keys[2:]):
        for column, key in zip(st.columns(2), row):
            with column, st.container(border=True, key='custom-template-' + key):
                st.markdown(f":material/{TEMPLATE_ICONS[key]}: **{TEMPLATES[key]['label']}**")
                st.caption(TEMPLATES[key]['audience'])
                chosen = current == key
                label = '已选用' if chosen else '选用'
                if run is None:
                    st.button(label, key='custom-pick-' + key, on_click=pick_template, args=(key,), disabled=busy,
                              type='primary' if chosen else 'secondary', width='stretch')
                elif st.button(label, key=f"custom-switch-{run['id']}-{key}", disabled=busy or chosen,
                               type='primary' if chosen else 'secondary', width='stretch'):
                    st.session_state[pending_key(run)] = key
                    st.rerun()
    if current:
        st.caption('关注点：' + TEMPLATES[current]['focus'])
    if run:
        confirm_switch(engine, run, busy)


def merchant_panel(engine, run, busy):
    """Existing task: summary. New task: enterprise profile or manual fields, returned for task creation."""
    panel_title('企业信息')
    if run:
        st.markdown(f"**{escape(run['inputs']['industry'])}**" + (f" · {escape(run['inputs']['brand'])}" if run['inputs'].get('brand') else ''))
        st.caption(run['inputs']['capabilities'])
        for line in structured_lines(run['inputs']):
            st.caption(line)
        return None
    profile_id, fields = enterprise_picker(engine, 'custom-new-profile', busy)
    key = f'custom-new-{profile_id}-'
    merchant = {'industry': st.text_input('行业（必填）', fields.get('industry', ''), max_chars=4000, key=key + 'industry', disabled=busy),
                'capabilities': st.text_area('生产能力（必填）', fields.get('capabilities', ''), max_chars=4000, key=key + 'capabilities', disabled=busy, height=90)}
    with st.expander('品牌、限制、产能、预算与使用日期（选填）'):
        merchant['brand'] = st.text_input('品牌名称', fields.get('brand', ''), max_chars=4000, key=key + 'brand', disabled=busy)
        merchant['constraints'] = st.text_area('已知限制', fields.get('constraints', ''), max_chars=4000, key=key + 'constraints', disabled=busy)
        picked = st.date_input('使用日期', value=None, key=key + 'use-date', disabled=busy,
                               help='如婚期、校庆日或交付日期，用于倒排生产与设计交付。')
        merchant['use_date'] = picked.isoformat() if picked else ''
        merchant['capacity'], merchant['budget'] = capacity_form({'capacity': fields.get('capacity', {})}, key, busy, wrap=False)
    return merchant


def transcript(run):
    """Chat history rebuilt from saved requirement versions and analyses; nothing extra is stored."""
    items = [('user', run['summaries'][0]['inputs']['request'])]
    answered, previous = {}, run['summaries'][0]['answers']
    for summary in run['summaries'][1:]:
        answered[summary['revision']] = {q: a for q, a in summary['answers'].items() if previous.get(q) != a}
        previous = summary['answers']
    for analysis in run['clarifications']:
        text = '已识别：' + ('；'.join(analysis['known']) or '暂无明确信息')
        if analysis.get('unknown'):
            text += '\n\n待探索：' + '；'.join(analysis['unknown'])
        items.append(('assistant', text))
        items += [('user', f'{q}：{a}') for q, a in answered.get(analysis['revision'] + 1, {}).items()]
    return items


def missing_reason(engine, merchant, template_needed=False):
    """Why a new task cannot be started yet; shown next to the disabled control instead of after submitting."""
    if not engine.workbench.llm.configured:
        return '需要先在系统设置中配置文字模型。'
    if template_needed and not st.session_state.get('custom-template'):
        return '请先在上方选择一个场景模板。'
    missing = [label for field, label in (('industry', '行业'), ('capabilities', '生产能力')) if not (merchant or {}).get(field, '').strip()]
    return f"请先填写{'和'.join(missing)}。" if missing else ''


def start_panel(engine, merchant, busy):
    template = st.session_state.get('custom-template')
    panel_title('开始定制')
    note = st.text_area('需求说明（选填）', max_chars=4000, key='custom-start-note', disabled=busy, height=80,
                        placeholder='例如：给 2006 届校友做 30 周年返校纪念，预算中档。不填也可以，模型会通过对话追问。')
    reason = missing_reason(engine, merchant, template_needed=True)
    label = f"用「{TEMPLATES[template]['label']}」模板开始" if template else '用模板开始'
    if st.button(label, type='primary', icon=':material/play_arrow:', key='custom-start-template', disabled=busy or bool(reason), width='stretch'):
        create_from_chat(engine, note.strip() or f"定制{TEMPLATES[template]['label']}", merchant)
    st.caption(reason or '创建后模型会分析需求，有不清楚的地方在对话中追问。也可以不选模板，直接在对话框描述需求。')


def create_from_chat(engine, text, merchant):
    template = st.session_state.get('custom-template') or ''
    prefill = {k: TEMPLATES[template][k] for k in TEMPLATE_FIELDS} if template else {}
    inputs = {'request': text, **merchant, **prefill, 'template': template, 'path': 'custom', 'days': 7}
    ok, run = action(engine, engine.workbench.demands.create, inputs)
    if ok:
        st.session_state.update({'custom-new': False, 'custom-task-url': run['id']})
        st.session_state.pop('custom-start-note', None)
        st.query_params['demand'] = run['id']
        launch(engine, 'demand_analyze', run['id'])


def answer(engine, run, session, index, choice, text, unknown):
    service = engine.workbench.demands
    last = index == len(session['questions']) - 1
    ok, _ = action(engine, service.save_answer, run['id'], session['id'], index, choice or '', text, unknown, index + 1)
    if ok:
        if last:
            launch(engine, 'demand_analyze', run['id'], session_id=session['id'])
        st.rerun()


def supplement(engine, run, text):
    """A free chat message after the questions: saved as a numbered supplement, then analysed again."""
    answers = dict(run['summaries'][-1]['answers'])
    answers[f"补充说明{1 + sum(k.startswith('补充说明') for k in answers)}"] = text
    ok, _ = action(engine, engine.workbench.demands.update, run['id'], run['inputs'], answers)
    if ok:
        if len(clarification_rounds(run)) < MAX_CLARIFICATION_ROUNDS:
            launch(engine, 'demand_analyze', run['id'])
        st.rerun()


def chat_panel(engine, run, merchant, busy):
    service = engine.workbench.demands
    configured = engine.workbench.llm.configured
    panel_title('人机对话')
    session = None
    with st.container(height=440, key='custom-chat-log'):
        if run is None:
            with st.chat_message('assistant'):
                st.write('请描述您的定制需求，例如：我要做校友纪念类产品。左侧选择场景模板、填写企业信息后发送。')
        else:
            for role, text in transcript(run):
                with st.chat_message(role):
                    st.write(text)
            analysis = latest_analysis(run)
            if not busy and can_answer(run, analysis):
                ok, session = action(engine, service.clarification_session, run['id'], analysis['id'])
                session = session if ok else None
            if session:
                index = session['cursor']
                q = session['questions'][index]
                saved = session['answers'].get(q['id'], {})
                key = f"custom-q-{session['id']}-{q['id']}-"
                with st.chat_message('assistant'):
                    st.write(f"第 {session['round']} 轮 · 第 {index + 1}/{len(session['questions'])} 题：{q['text']}")
                    choice = st.pills('可选回答', q['options'], default=saved.get('choice') or None, key=key + 'choice') if q['options'] else None
                    text = st.text_input('补充或其他回答', saved.get('text', ''), max_chars=4000, key=key + 'text')
                    unknown = st.checkbox('暂不确定，请给建议', saved.get('unknown', False), key=key + 'unknown')
                    a, b = st.columns(2)
                    if a.button('上一题', key=key + 'back', disabled=index == 0, width='stretch'):
                        if action(engine, service.move_question, run['id'], session['id'], index - 1)[0]:
                            st.rerun()
                    last = index == len(session['questions']) - 1
                    if b.button('提交本轮回答' if last else '下一题', key=key + 'next', type='primary', width='stretch'):
                        answer(engine, run, session, index, choice, text, unknown)
            elif run['approved_revision'] == run['revision']:
                with st.chat_message('assistant'):
                    st.write('需求已确认。下一步：生成产品方向。')
            elif analysis and not busy:
                with st.chat_message('assistant'):
                    st.write('请检查右侧的需求结构化预览，采用合适的建议后确认需求；也可以继续补充说明。')
            step_feedback(engine, run['id'], last_op(run), ('demand_analyze',), 'custom-chat')
    placeholder = '回答当前问题或补充说明' if session else '描述需求或补充说明'
    reason = missing_reason(engine, merchant) if run is None else ('' if configured else '对话分析需要先在系统设置中配置文字模型。')
    message = st.chat_input(placeholder, key='custom-chat-input', max_chars=4000, disabled=busy or bool(reason))
    if reason:
        st.caption(reason)
    if message and message.strip():
        if run is None:
            create_from_chat(engine, message.strip(), merchant)
        elif session:
            answer(engine, run, session, session['cursor'], st.session_state.get(f"custom-q-{session['id']}-{session['questions'][session['cursor']]['id']}-choice"), message.strip(), False)
        else:
            supplement(engine, run, message.strip())


def preview_panel(engine, run, busy, editing=False):
    panel_title('需求结构化预览')
    if run is None:
        template = st.session_state.get('custom-template')
        values = TEMPLATES[template] if template else {}
        st.table([{'项目': FIELDS[k], '内容': values.get(k) or '待对话识别'} for k in TEMPLATE_FIELDS])
        st.caption('发送第一条消息后创建任务；模板预填只是草稿，可在确认前修改。')
        return
    inputs = run['inputs']
    rows = [{'项目': FIELDS[k], '内容': inputs.get(k) or '未提供'} for k in TEMPLATE_FIELDS]
    rows += [{'项目': line.split('：', 1)[0], '内容': line.split('：', 1)[1]} for line in structured_lines(inputs)]
    st.table(rows)
    if run['approved_revision'] == run['revision'] and not editing:
        st.success('需求已确认。')
        return
    if busy:
        st.caption('任务执行中，完成后可确认需求。')
        return
    confirmation_view(engine, run, latest_analysis(run), inline=True, auto_search=False)


def need_step(engine, run, busy, editing=False):
    if editing:
        flow.impact(run, IMPACT[0], '设计说明')
    left, middle, right = st.columns([1, 1.35, 1.1], gap='medium')
    with left, st.container(border=True, key='custom-templates'):
        template_panel(engine, run, busy)
        merchant = merchant_panel(engine, run, busy)
        if run is None:
            start_panel(engine, merchant, busy)
    with middle, st.container(border=True, key='custom-chat'):
        chat_panel(engine, run, merchant, busy)
    with right, st.container(border=True, key='custom-preview'):
        preview_panel(engine, run, busy, editing)


def need_lines(run):
    inputs = run['inputs']
    return [inputs['request'], *(f"{FIELDS[k]}：{inputs[k]}" for k in TEMPLATE_FIELDS if inputs.get(k)), *structured_lines(inputs)]


# ② 产品方向

def picked(run):
    st.session_state.pop(edit_key(run, 1), None)
    flow.flash(PREFIX, '已选定方向。下一步：选择文化元素并生成设计说明。')


def direction_step(engine, run, busy, editing):
    work = engine.workbench
    batch = next((b for b in reversed(run['candidate_rounds']) if not stale(run, b)), None)
    step_feedback(engine, run['id'], last_op(run), ('demand_candidates',), 'custom-candidates')
    if running(run, ('demand_candidates',)):
        return
    if editing:
        flow.impact(run, IMPACT[1], '设计说明')
    if batch is None:
        st.caption('依据已确认的人群、场景、用途与文化主题，结合场景模板和文化知识库生成产品方向；不使用热点搜索。')
        if st.button('生成产品方向', type='primary', icon=':material/auto_awesome:', disabled=busy or not work.llm.configured,
                     key=f"custom-generate-{run['id']}"):
            launch(engine, 'demand_candidates', run['id'], count=3)
    else:
        candidate_cards(engine, run, batch, busy, after_select=lambda: picked(run))
        if batch.get('capacity_log'):
            with st.expander(f"产能检查记录 · {len(batch['capacity_log'])} 个方向已自动修正"):
                for item in batch['capacity_log']:
                    st.write(item['name'] + '：' + '；'.join(item['problems']))
    with st.expander('更多：按数量和意见重新生成、参考资料'):
        key = f"custom-regen-{run['id']}-"
        with st.form(key + 'form'):
            a, b = st.columns([1, 3], vertical_alignment='bottom')
            count = a.selectbox('方向数量', [3, 5], key=key + 'count')
            instruction = b.text_input('调整意见（选填）', max_chars=4000, key=key + 'instruction')
            regen = st.form_submit_button('重新生成产品方向', disabled=busy or not work.llm.configured)
        if regen:
            launch(engine, 'demand_candidates', run['id'], count=count, instruction=instruction)
        if batch:
            evidence_view(batch['context']['evidence'])


# ③ 文化元素与设计说明

def designer_panel(engine, run, busy):
    service = engine.workbench.demands
    batch = next(r for r in run['candidate_rounds'] if r['id'] == run['selection']['round_id'])
    elements = {k: v for k, v in batch['context']['evidence'].items() if 'element_id' in v}
    picks = run.get('element_picks') if (run.get('element_picks') or {}).get('selection') == run['selection'] else {}
    key = f"custom-pick-{run['id']}-{run['selection']['round_id']}-{run['selection']['candidate_id']}-"
    motifs = {k: v for k, v in elements.items() if v['type'] != 'palette'}
    palettes = {k: v for k, v in elements.items() if v['type'] == 'palette'}
    panel_title('设计师选择面板')
    st.markdown('**文化元素选择**')
    chosen = []
    if motifs:
        chosen = st.pills('文化元素', list(motifs), selection_mode='multi', default=[k for k in picks.get('adopt', []) if k in motifs],
                          format_func=lambda k: motifs[k]['label'], key=key + 'motifs', label_visibility='collapsed', disabled=busy) or []
    else:
        st.caption('本轮没有匹配的纹样或文化元素，可在知识库解码相关资料后重新生成方向。' if page_enabled(engine, 'culture')
                   else '本轮没有匹配的纹样或文化元素，设计说明会按需求给出图案与配色建议。')
    st.markdown('**配色方案**')
    chosen_palettes = []
    if palettes:
        for item in palettes.values():
            st.markdown(f"{escape(item['label'])}：" + swatches(item['colors']), unsafe_allow_html=True)
        chosen_palettes = st.pills('配色方案', list(palettes), selection_mode='multi', default=[k for k in picks.get('adopt', []) if k in palettes],
                                   format_func=lambda k: palettes[k]['label'], key=key + 'palettes', label_visibility='collapsed', disabled=busy) or []
    else:
        st.caption('本轮没有匹配的配色素材，设计说明会给出建议配色。')
    style = st.pills('风格', STYLES, default=picks.get('style') or None, key=key + 'style', disabled=busy)
    note = st.text_input('补充说明（选填）', picks.get('note', ''), max_chars=1000, key=key + 'note', disabled=busy)
    adopt = [*chosen, *chosen_palettes]
    st.caption('选中的素材会要求在设计说明中标注引用编号；设计解读类素材只作创意依据。')
    a, b = st.columns([1, 2])
    if a.button('保存选择', key=key + 'save', disabled=busy, width='stretch', help='只保存当前选择，不调用模型。'):
        if action(engine, service.save_picks, run['id'], {'adopt': adopt, 'style': style or '', 'note': note})[0]:
            st.toast('已保存设计选择。')
    if b.button('生成设计说明', key=key + 'brief', type='primary', icon=':material/description:', width='stretch',
                disabled=busy or not engine.workbench.llm.configured):
        st.session_state.pop(edit_key(run, 2), None)
        launch(engine, 'demand_brief', run['id'], element_changes={'adopt': adopt, 'style': style or ''}, instruction=note)


def pick_lines(run):
    picks = run.get('element_picks') or {}
    if picks.get('selection') != run['selection'] or not (picks.get('adopt') or picks.get('style')):
        return []
    return [f"已选素材 {len(picks.get('adopt', []))} 项" + (f" · 风格：{picks['style']}" if picks.get('style') else '')]


def element_step(engine, run, brief, busy, editing, advance_key):
    step_feedback(engine, run['id'], last_op(run), ('demand_brief', 'demand_revise'), 'custom-brief')
    if running(run, ('demand_brief', 'demand_revise')):
        return
    if brief is None:
        st.caption('已选方向：' + '；'.join(direction_lines(run)))
        designer_panel(engine, run, busy)
        return
    brief_content(run, brief)
    if advance_key and st.button('下一步：设计方案', type='primary', icon=':material/arrow_forward:', key=advance_key + '-button'):
        st.session_state[advance_key] = True
        st.rerun()
    with st.expander('重新选择文化元素并生成新的设计说明', expanded=editing):
        if editing:
            flow.impact(run, IMPACT[2], '设计说明')
        designer_panel(engine, run, busy)
    flow.brief_more(engine, run, brief, busy, False, PREFIX, edit_key(run, 2), '设计说明')


# Page

def custom_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('特色个性化产品定制工作台', '按「需求对话 → 产品方向 → 文化元素与设计说明 → 设计方案」四步推进，面向婚礼、校服、校友纪念品、文旅伴手礼等场景。', 'CUSTOM DESIGN')
    flow.show_flash(PREFIX)
    if st.query_params.get('demand'):
        st.session_state['custom-new'] = False
    has_runs = any(r['inputs']['path'] == 'custom' for r in work.demands.runs())
    new = st.session_state.get('custom-new', False) or not has_runs
    a, b = st.columns([4, 1], vertical_alignment='bottom')
    run = None
    with a:
        if new:
            st.markdown('**新建定制任务**：选场景模板并填写企业信息后点「用模板开始」，或直接在对话框描述需求。')
        else:
            run = task_select(engine, 'custom', '定制任务')
    if new and has_runs:
        if b.button('返回已有任务', width='stretch'):
            st.session_state['custom-new'] = False
            st.rerun()
    elif not new and b.button('新建定制任务', icon=':material/add:', disabled=busy, width='stretch'):
        st.session_state['custom-new'] = True
        st.query_params.pop('demand', None)
        st.rerun()
    if not work.llm.configured:
        st.info('对话分析和生成前，请在「系统 → 模型、企业与日历设置」中配置文字模型。')
    if run is None:
        flow.flow_bar(STEPS, ['current', 'locked', 'locked', 'locked'], '定制流程')
        with st.container(border=True, key='flow-step-0'):
            flow.step_title(STEPS, 0, 'current', '用模板开始或在对话框描述需求')
            need_step(engine, None, busy)
        for i in range(1, len(STEPS)):
            with st.container(border=True, key=f'flow-step-{i}'):
                flow.step_title(STEPS, i, 'locked', f'需先完成第 {NUMBERS[i - 1]} 步')
        return
    brief = current_brief(run)
    advance_key = f"custom-advance-{brief['id']}" if brief else None
    done, current, brief, design = custom_state(run, bool(advance_key and st.session_state.get(advance_key)))
    states = ['done' if done[i] else 'current' if i == current else 'locked' for i in range(len(STEPS))]
    flow.flow_bar(STEPS, states, '定制流程')
    requested = st.query_params.get('step')
    summaries = {0: lambda: need_lines(run), 1: lambda: direction_lines(run), 2: lambda: [*flow.brief_lines(brief), *pick_lines(run)]}
    for i, name in enumerate(STEPS):
        with st.container(border=True, key=f'flow-step-{i}'):
            if states[i] == 'locked':
                flow.step_title(STEPS, i, 'locked', f'需先完成第 {NUMBERS[i - 1]} 步')
                continue
            editing = done[i] and st.session_state.get(edit_key(run, i), False)
            expanded = states[i] == 'current' or editing or i == 3 or requested == name
            title, edit = st.columns([6, 1], vertical_alignment='center')
            with title:
                flow.step_title(STEPS, i, states[i])
            if done[i] and i < 3:
                edit.button('取消修改' if editing else '修改', key=edit_key(run, i) + '-button', on_click=flow.toggle, args=(edit_key(run, i),),
                            disabled=busy, width='stretch')
            if not expanded:
                for line in summaries[i]():
                    st.caption(line)
                continue
            if i == 0:
                need_step(engine, run, busy, editing)
            elif i == 1:
                direction_step(engine, run, busy, editing)
            elif i == 2:
                element_step(engine, run, brief, busy, editing, None if done[2] else advance_key)
            else:
                flow.design_view(engine, run, brief, design, busy, PREFIX, default_variants=3)
    flow.version_record(run, '设计说明')
    boundary(['定制设计不使用热点搜索，依据为需求对话、场景模板、企业产能和文化知识库。',
              '模板预填和模型整理的人群、场景、用途只是草稿，确认后才成为需求条件。',
              '文化元素与配色来自知识库：「资料记载」可引用原文，「设计解读」只作创意依据；HEX 色值不代表印刷定色。',
              '三版设计方案只生成文字，确认一版后再生成图稿；生成内容需设计师复核，不承诺生产可行性。'])
