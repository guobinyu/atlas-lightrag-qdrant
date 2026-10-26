"""Custom design workbench (proposal sketch 2): scene templates, chat clarification, designer selections, confirmed proposals."""
from html import escape

import streamlit as st

from lab.demand_presets import STYLES, TEMPLATES, TEMPLATE_FIELDS
from lab.demand_ui import (action, brief_step, candidate_cards, capacity_form, confirmation_view, design_cards, design_step,
                           enterprise_picker, evidence_view, history_view, launch, round_topics_caption, step_feedback,
                           task_select)
from lab.demands import FIELDS, MAX_CLARIFICATION_ROUNDS, can_answer, clarification_rounds, stale, structured_lines
from lab.ui_kit import boundary, panel_title, step_tabs, swatches

STEPS = ['需求对话', '方向与元素', '方案确认']
TEMPLATE_ICONS = {'wedding': 'celebration', 'uniform': 'school', 'alumni': 'diversity_3', 'souvenir': 'card_giftcard'}


def last_op(run):
    ops = list(run['operations'].values())
    return ops[-1] if ops else None


def pick_template(key):
    current = st.session_state.get('custom-template')
    st.session_state['custom-template'] = None if current == key else key


def switch_template(engine, run, key):
    """Existing task: record the template and prefill only empty fields; this creates a new requirement version."""
    inputs = {**run['inputs'], 'template': key}
    for field in TEMPLATE_FIELDS:
        inputs[field] = inputs.get(field) or TEMPLATES[key][field]
    ok, _ = action(engine, engine.workbench.demands.update, run['id'], inputs, run['summaries'][-1]['answers'])
    if ok:
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
                    switch_template(engine, run, key)
    if current:
        st.caption('关注点：' + TEMPLATES[current]['focus'])


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
    with st.expander('品牌、限制、产能与预算（选填）'):
        merchant['brand'] = st.text_input('品牌名称', fields.get('brand', ''), max_chars=4000, key=key + 'brand', disabled=busy)
        merchant['constraints'] = st.text_area('已知限制', fields.get('constraints', ''), max_chars=4000, key=key + 'constraints', disabled=busy)
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


def create_from_chat(engine, text, merchant):
    template = st.session_state.get('custom-template') or ''
    prefill = {k: TEMPLATES[template][k] for k in TEMPLATE_FIELDS} if template else {}
    inputs = {'request': text, **merchant, **prefill, 'template': template, 'path': 'custom', 'days': 7}
    ok, run = action(engine, engine.workbench.demands.create, inputs)
    if ok:
        st.session_state.update({'custom-new': False, 'custom-task': run['id'], 'custom-task-url': run['id']})
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
            analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
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
                    st.write('需求已确认。请到「方向与元素」生成产品方向并选择文化元素与配色。')
            elif analysis and not busy:
                with st.chat_message('assistant'):
                    st.write('请检查右侧的需求结构化预览，采用合适的建议后确认需求；也可以继续补充说明。')
            step_feedback(engine, run['id'], last_op(run), ('demand_analyze',), 'custom-chat')
    placeholder = '回答当前问题或补充说明' if session else '描述需求或补充说明'
    message = st.chat_input(placeholder, key='custom-chat-input', max_chars=4000, disabled=busy or not configured)
    if not configured:
        st.caption('对话分析需要先在系统设置中配置文字模型。')
    if message and message.strip():
        if run is None:
            create_from_chat(engine, message.strip(), merchant)
        elif session:
            answer(engine, run, session, session['cursor'], st.session_state.get(f"custom-q-{session['id']}-{session['questions'][session['cursor']]['id']}-choice"), message.strip(), False)
        else:
            supplement(engine, run, message.strip())


def preview_panel(engine, run, busy):
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
    if run['approved_revision'] == run['revision']:
        st.success('需求已确认，可以进入「方向与元素」。')
        return
    if busy:
        st.caption('任务执行中，完成后可确认需求。')
        return
    analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
    confirmation_view(engine, run, analysis, inline=True)


def designer_panel(engine, run, busy):
    service = engine.workbench.demands
    panel_title('设计师选择面板')
    if not run['selection']:
        st.caption('先在左侧选定一个产品方向。')
        return
    batch = next(r for r in run['candidate_rounds'] if r['id'] == run['selection']['round_id'])
    elements = {k: v for k, v in batch['context']['evidence'].items() if 'element_id' in v}
    picks = run.get('element_picks') if (run.get('element_picks') or {}).get('selection') == run['selection'] else {}
    key = f"custom-pick-{run['id']}-{run['selection']['round_id']}-{run['selection']['candidate_id']}-"
    motifs = {k: v for k, v in elements.items() if v['type'] != 'palette'}
    palettes = {k: v for k, v in elements.items() if v['type'] == 'palette'}
    st.markdown('**文化元素选择**')
    chosen = []
    if motifs:
        chosen = st.pills('文化元素', list(motifs), selection_mode='multi', default=[k for k in picks.get('adopt', []) if k in motifs],
                          format_func=lambda k: motifs[k]['label'], key=key + 'motifs', label_visibility='collapsed', disabled=busy) or []
    else:
        st.caption('本轮没有匹配的纹样或文化元素，可在知识库解码相关资料后重新生成方向。')
    st.markdown('**配色方案**')
    chosen_palettes = []
    if palettes:
        for ref, item in palettes.items():
            st.markdown(f"{escape(item['label'])}：" + swatches(item['colors']), unsafe_allow_html=True)
        chosen_palettes = st.pills('配色方案', list(palettes), selection_mode='multi', default=[k for k in picks.get('adopt', []) if k in palettes],
                                   format_func=lambda k: palettes[k]['label'], key=key + 'palettes', label_visibility='collapsed', disabled=busy) or []
    else:
        st.caption('本轮没有匹配的配色素材，设计说明会给出建议配色。')
    style = st.pills('风格', STYLES, default=picks.get('style') or None, key=key + 'style', disabled=busy)
    note = st.text_input('保存说明', picks.get('note', ''), max_chars=1000, key=key + 'note', disabled=busy)
    adopt = [*chosen, *chosen_palettes]
    a, b = st.columns(2)
    if a.button('保存需求', key=key + 'save', disabled=busy, width='stretch'):
        ok, _ = action(engine, service.save_picks, run['id'], {'adopt': adopt, 'style': style or '', 'note': note})
        if ok:
            st.toast('已保存设计选择。')
    if b.button('生成设计说明', key=key + 'brief', type='primary', width='stretch',
                disabled=busy or not engine.workbench.llm.configured or run['approved_revision'] != run['revision']):
        launch(engine, 'demand_brief', run['id'], element_changes={'adopt': adopt, 'style': style or ''}, instruction=note)
    st.caption('选中的素材会要求在设计说明中标注引用编号；设计解读类素材只作创意依据。')
    step_feedback(engine, run['id'], last_op(run), ('demand_brief',), 'custom-brief')


def directions_step(engine, run, busy):
    service = engine.workbench.demands
    approved = run['approved_revision'] == run['revision']
    left, right = st.columns([1.4, 1], gap='medium')
    with left, st.container(border=True, key='custom-directions'):
        panel_title('产品方向')
        if not approved:
            st.caption('请先在「需求对话」中确认需求。')
        with st.form(f"custom-generate-{run['id']}"):
            a, b = st.columns([1, 3], vertical_alignment='bottom')
            count = a.selectbox('方向数量', [3, 5])
            instruction = b.text_input('调整意见（选填）', max_chars=4000)
            generate = st.form_submit_button('生成产品方向', type='primary', disabled=busy or not engine.workbench.llm.configured or not approved)
        if generate:
            launch(engine, 'demand_candidates', run['id'], count=count, instruction=instruction)
        step_feedback(engine, run['id'], last_op(run), ('demand_candidates',), 'custom-candidates')
        if run['candidate_rounds']:
            from lab.campaign_ui import local_time
            batches = {b['id']: b for b in reversed(run['candidate_rounds'])}
            bid = st.selectbox('方向轮次', list(batches), key=f"custom-round-{run['id']}-{len(batches)}",
                               format_func=lambda k: f"{local_time(batches[k]['at'])} · {k[:6]}" + (' · 历史条件' if stale(run, batches[k]) else ''))
            batch = batches[bid]
            round_topics_caption(batch)
            candidate_cards(engine, run, batch, busy)
            with st.expander('本轮参考资料'):
                evidence_view(batch['context']['evidence'])
    with right, st.container(border=True, key='custom-designer'):
        designer_panel(engine, run, busy)


def confirm_step(engine, run):
    busy = engine.workbench.active is not None
    prefix = f"demand-{run['id']}-r{run['revision']}-"
    brief = brief_step(engine, run, prefix, last_op(run), heading='设计说明', generate=False)
    design_cards(engine, run, brief, busy)
    design_step(engine, run, brief, last_op(run), heading='方案生成与修订', default_variants=3)


def custom_page(engine):
    from lab.workbench_ui import header
    work = engine.workbench
    busy = work.active is not None
    header('特色个性化产品定制工作台', '面向婚礼、校服、校友纪念品、文旅伴手礼等场景，通过多轮对话拆解需求，选择文化元素与配色，生成多版设计方案供设计师确认。', 'CUSTOM DESIGN')
    if st.query_params.get('demand'):
        st.session_state['custom-new'] = False
    has_runs = any(r['inputs']['path'] == 'custom' for r in work.demands.runs())
    new = st.session_state.get('custom-new', False) or not has_runs
    a, b = st.columns([4, 1], vertical_alignment='bottom')
    run = None
    with a:
        if new:
            st.markdown('**新建定制任务**：在下方对话中发送第一条需求即可创建。')
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
    dialogue, directions, confirm = step_tabs(STEPS, 'custom-steps')
    with dialogue:
        left, middle, right = st.columns([1, 1.35, 1.1], gap='medium')
        with left, st.container(border=True, key='custom-templates'):
            template_panel(engine, run, busy)
            merchant = merchant_panel(engine, run, busy)
        with middle, st.container(border=True, key='custom-chat'):
            chat_panel(engine, run, merchant, busy)
        with right, st.container(border=True, key='custom-preview'):
            preview_panel(engine, run, busy)
    with directions:
        if run:
            directions_step(engine, run, busy)
        else:
            st.caption('创建任务并确认需求后，在这里生成产品方向。')
    with confirm:
        if run:
            confirm_step(engine, run)
        else:
            st.caption('生成设计说明后，在这里比较并确认设计方案。')
    if run:
        with st.expander('需求与执行历史'):
            history_view(run)
    boundary(['模板预填和模型整理的人群、场景、用途只是草稿，确认后才成为需求条件。',
              '文化元素与配色来自知识库：「资料记载」可引用原文，「设计解读」只作创意依据；HEX 色值不代表印刷定色。',
              '三版设计方案只生成文字，确认一版后再生成图稿；生成内容需设计师复核，不承诺生产可行性。'])
