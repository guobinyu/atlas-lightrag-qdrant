"""A staged product exploration UI; ordinary editing never calls the model."""
import json
from copy import deepcopy
from html import escape
import streamlit as st

from lab.demands import MAX_CLARIFICATION_ROUNDS, clarification_rounds, can_answer, clean_inputs, adoption_value, FIELDS, BRIEF_FIELDS, stale, export_bundle, export_markdown, brief_sections, candidate_comparison, mapping_markdown
from lab.campaign_ui import local_time

PATHS = {'trend': '结合近期话题', 'custom': '人群／场景定制'}
STATES = {'running': '执行中', 'success': '已完成', 'failed': '失败', 'interrupted': '已中断'}


def action(engine, function, *args, **kwargs):
    try:
        return True, function(*args, **kwargs)
    except Exception as exc:
        from lab.engine import safe_error
        message = safe_error(exc, engine.settings)
        key = engine.workbench.llm.settings.api_key
        st.error(message.replace(key, '[redacted]') if key else message)
        return False, None


def launch(engine, kind, identifier, **inputs):
    ok, _ = action(engine, engine.workbench.start, kind, {'run_id': identifier, **inputs})
    if ok:
        st.rerun()


def input_form(values, prefix, disabled=False):
    result = {}
    result['request'] = st.text_area('原始需求', value=values.get('request', ''), key=prefix + 'request', max_chars=4000, disabled=disabled)
    a, b = st.columns(2)
    result['industry'] = a.text_input('行业（必填）', value=values.get('industry', ''), key=prefix + 'industry', max_chars=4000, disabled=disabled)
    result['brand'] = b.text_input('品牌名称（选填）', value=values.get('brand', ''), key=prefix + 'brand', max_chars=4000, disabled=disabled)
    result['capabilities'] = st.text_area('生产能力（必填）', value=values.get('capabilities', ''), placeholder='例如：可生产真丝丝巾和家居服，支持数码印花。', key=prefix + 'capabilities', max_chars=4000, disabled=disabled)
    result['constraints'] = st.text_area('已知限制（选填）', value=values.get('constraints', ''), placeholder='材料、工艺、价格范围和不能做的产品。', key=prefix + 'constraints', max_chars=4000, disabled=disabled)
    a, b = st.columns(2)
    result['path'] = a.selectbox('探索路径', list(PATHS), index=list(PATHS).index(values.get('path', 'trend')), format_func=PATHS.get, key=prefix + 'path', disabled=disabled)
    result['days'] = b.selectbox('话题搜索时间范围（仅近期话题路径使用）', [1, 7, 30], index=[1, 7, 30].index(values.get('days', 7)), format_func=lambda n: '最近 24 小时' if n == 1 else f'最近 {n} 天', key=prefix + 'days', disabled=disabled)
    for key in ('audience', 'scene', 'purpose', 'theme'):
        result[key] = st.text_input(FIELDS[key], value=values.get(key, ''), key=prefix + key, max_chars=4000, disabled=disabled)
    st.caption('定制路径至少填写目标人群、使用场景或文化主题中的一项。品牌未填写时保留为“未提供”。')
    return result


def evidence_view(evidence):
    if not evidence:
        st.caption('没有可引用的资料，以下方向属于策划建议。')
    for ref, item in evidence.items():
        with st.expander(f"[{ref}] {item.get('title') or item.get('file_path') or '知识库片段'}"):
            if item.get('url'):
                st.link_button('打开来源', item['url'])
            st.text(item.get('text') or item.get('content', ''))
            if item.get('analysis_truncated'):
                st.caption('本次分析使用正文前 9,000 字符。')


def facts_view(facts):
    for fact in facts:
        st.write(fact['text'])
        for cite in fact['citations']:
            st.caption('[' + cite['ref'] + '] ' + cite['quote'])


def open_dialog(identifier, stage):
    st.session_state['demand-dialog'] = {'id': identifier, 'stage': stage}
    st.rerun()


def close_dialog():
    st.session_state.pop('demand-dialog', None)
    st.session_state.pop('demand-await', None)
    for key in list(st.session_state):
        if key.startswith('demand-confirm-'):
            del st.session_state[key]


OP_TARGETS = {
    'demand_analyze': ('demand-clarify', '需求澄清'),
    'demand_search': ('demand-research', '话题资料'),
    'demand_candidates': ('demand-candidates', '产品候选'),
    'demand_brief': ('demand-brief', '设计需求单'),
    'demand_revise': ('demand-revise', '需求单修订'),
    'demand_image_generate': ('demand-images', '设计图稿'),
    'demand_image_revise': ('demand-images', '图稿修改'),
    'demand_design_generate': ('demand-design', '设计方案与图稿'),
    'demand_design_revise': ('demand-design', '设计方案修订'),
}


def retry_operation(engine, identifier, op):
    ok, _ = action(engine, engine.workbench.start, 'demand_retry',
                   {'run_id': identifier, 'operation_id': op['id']})
    if ok:
        if op['kind'] == 'demand_analyze':
            st.session_state['demand-dialog'] = {'id': identifier, 'stage': 'waiting'}
            st.session_state['demand-await'] = {'id': identifier, 'op': op['id']}
        st.rerun()


@st.fragment(run_every=2)
def operation_progress(engine, identifier, operation_id, was_running, location):
    op = engine.workbench.demands.load(identifier)['operations'][operation_id]
    busy = engine.workbench.active is not None
    if was_running and op['status'] != 'running' and not busy:
        st.rerun(scope='app')
    with st.container(key=f'demand-feedback-{location}'):
        if op['status'] == 'running':
            st.info('正在' + op['stage'] + '…完成后将在此处显示结果。')
            st.caption('可以浏览已生成内容；请保持服务运行，无需重复提交。')
        elif op['status'] in ('failed', 'interrupted'):
            st.warning(op.get('message') or '操作未完成，请重试。')
            if st.button('重试失败阶段', key=f'demand-retry-{location}-{op["id"]}',
                         disabled=busy or not engine.workbench.llm.configured):
                retry_operation(engine, identifier, op)
        else:
            st.success(OP_TARGETS[op['kind']][1] + '已完成，请查看本步骤结果。')


def step_feedback(engine, identifier, op, kinds, location):
    if op and op['kind'] in kinds:
        operation_progress(engine, identifier, op['id'], op['status'] == 'running', location)


@st.fragment(run_every=2)
def fixed_status(engine, identifier, operation_id, was_running):
    op = engine.workbench.demands.load(identifier)['operations'][operation_id]
    if was_running and op['status'] != 'running' and engine.workbench.active is None:
        st.rerun(scope='app')
    target, label = OP_TARGETS[op['kind']]
    state = STATES[op['status']]
    message = '正在' + op['stage'] if op['status'] == 'running' else label + ' · ' + state
    st.markdown(f'''<style>
    .demand-fixed-status {{position:fixed;bottom:16px;right:24px;z-index:99;
      max-width:min(620px,calc(100vw - 48px));padding:12px 18px;border-radius:12px;
      background:#eaf3f2;color:#163d3a;border:1px solid #b8d5d0;
      box-shadow:0 4px 16px #0002;display:flex;gap:16px;align-items:center;}}
    .demand-fixed-status a {{color:#145b50;white-space:nowrap;font-weight:600;}}
    [id^="demand-"] {{scroll-margin-top:80px;}}
    </style><div class="demand-fixed-status" role="status" aria-live="polite"><span>{escape(message)}</span><a href="#{target}" target="_self">查看当前步骤</a></div>''', unsafe_allow_html=True)


def analyze_dialog(engine, identifier, **inputs):
    ok, result = action(engine, engine.workbench.start, 'demand_analyze', {'run_id': identifier, **inputs})
    if ok:
        st.session_state['demand-dialog'] = {'id': identifier, 'stage': 'waiting'}
        st.session_state['demand-await'] = {'id': identifier, 'op': result[1].value['operation_id']}
        st.rerun()


@st.dialog('完善产品需求', width='large', on_dismiss=close_dialog)
def clarification_dialog(engine, identifier, stage):
    service = engine.workbench.demands
    run = service.load(identifier)
    busy = engine.workbench.active is not None
    analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
    if stage == 'waiting':
        operations = [o for o in run['operations'].values() if o['kind'] == 'demand_analyze']
        if operations:
            op = operations[-1]
            st.caption('正在整理本轮需求。关闭窗口后任务仍会继续，可从需求澄清区域查看结果。')
            operation_progress(engine, identifier, op['id'], op['status'] == 'running', 'dialog')
        return
    st.caption('保存后可继续。关闭窗口会暂停；当前题未保存的输入可能在刷新后丢失。')
    if busy:
        st.info('任务执行中，请完成后继续。')
        return
    if stage == 'questions' and can_answer(run, analysis):
        ok, session = action(engine, service.clarification_session, identifier, analysis['id'])
        if not ok:
            return
        index = session['cursor']
        q = session['questions'][index]
        answer = session['answers'].get(q['id'], {})
        key = f"demand-dialog-{identifier}-{session['id']}-{q['id']}-"
        st.caption(f"第 {session['round']} 轮 · 本轮第 {index + 1} / {len(session['questions'])} 题")
        st.write(q['text'])
        with st.form(key + 'answer'):
            choice = st.radio('选择回答', q['options'], index=q['options'].index(answer['choice']) if answer.get('choice') in q['options'] else None, key=key + 'choice') if q['options'] else None
            text = st.text_area('补充说明／其他回答', value=answer.get('text', ''), max_chars=4000, key=key + 'text')
            unknown = st.checkbox('暂不确定，请给建议', value=answer.get('unknown', False), key=key + 'unknown')
            a, b = st.columns(2)
            previous = a.form_submit_button('上一题', disabled=index == 0)
            last = index == len(session['questions']) - 1
            forward = b.form_submit_button('提交本轮回答' if last else '保存并下一题', type='primary')
        if previous:
            if choice or text.strip() or unknown:
                ok, _ = action(engine, service.save_answer, identifier, session['id'], index, choice or '', text, unknown, index - 1)
            else:
                ok, _ = action(engine, service.move_question, identifier, session['id'], index - 1)
            if ok:
                st.rerun()
        if forward:
            ok, _ = action(engine, service.save_answer, identifier, session['id'], index, choice or '', text, unknown, index + 1)
            if ok:
                if last:
                    analyze_dialog(engine, identifier, session_id=session['id'])
                else:
                    st.rerun()
        if st.button('按当前需求继续'):
            open_dialog(identifier, 'confirm')
        return
    confirmation_view(engine, run, analysis)


def adopt_to_draft(prefix, analysis, kind, field):
    draft = st.session_state[prefix + 'draft']
    if draft['invalidated']:
        return
    value = adoption_value(analysis, kind, field)
    st.session_state[prefix + field] = value
    draft['adoptions'] = [a for a in draft['adoptions'] if a['field'] != field]
    draft['adoptions'].append({'analysis_id': analysis['id'], 'kind': kind, 'field': field})
    draft['expected'][field] = value


def confirmation_view(engine, run, analysis):
    service = engine.workbench.demands
    identifier = run['id']
    prefix = f"demand-confirm-{identifier}-{run['revision']}-"
    draft = st.session_state.setdefault(prefix + 'draft', {
        'expected': clean_inputs(run['inputs']), 'adoptions': [], 'invalidated': False})
    st.write('确认需求摘要')
    st.caption('采用到摘要仅修改当前草稿；点击确认后才保存。关闭窗口不会批准需求，未确认草稿不保留。')
    if len(clarification_rounds(run)) >= MAX_CLARIFICATION_ROUNDS:
        st.info('请检查并确认需求摘要，未明确的内容保留为待探索项。')
    with st.expander('用户已提供内容'):
        for field, label in FIELDS.items():
            st.write(f"{label}：{run['inputs'].get(field) or '未提供'}")
        for question, answer in service.draft_answers(run).items():
            st.write(question + '：' + answer)
    st.write('可编辑需求摘要')
    values = input_form(run['inputs'], prefix)
    # These widgets are deliberately outside a form: edits must invalidate old
    # recommendations before another recommendation can be adopted.
    if clean_inputs(values) != draft['expected']:
        draft['invalidated'] = True
        draft['adoptions'] = []
    answers_changed = service.draft_answers(run) != run['summaries'][-1]['answers']
    applicable = bool(analysis and analysis['revision'] == run['revision'] and not draft['invalidated'] and not answers_changed)
    if analysis:
        if not applicable:
            st.info('需求或回答已改变，以下为原分析记录，不能继续采纳。可直接确认当前摘要，或根据最新资料重新分析。')
        for kind, title in [('suggestions', '系统整理的需求理解'), ('recommendations', '系统推荐的探索方向')]:
            st.write(title)
            items = analysis.get(kind, {})
            if not items:
                st.caption('暂无需要采纳的内容。')
            for field, item in items.items():
                value = adoption_value(analysis, kind, field)
                st.write(f"{FIELDS[field]}：{value}")
                if kind == 'recommendations':
                    st.caption('建议理由：' + item['reason'])
                adopted = any(a['field'] == field and a['kind'] == kind for a in draft['adoptions'])
                st.button('已采用到草稿' if adopted else '采用到摘要', key=prefix + kind + '-' + field,
                          disabled=not applicable or adopted, on_click=adopt_to_draft,
                          args=(prefix, analysis, kind, field))
        st.write('仍待探索内容（原分析记录）' if not applicable else '仍待探索内容')
        st.write('；'.join(analysis['unknown']) or '暂无')
    st.caption('部分条件暂不确定仍可继续；设计细节由后续产品建议展开。')
    if st.button('确认需求并继续', type='primary'):
        ok, updated = action(engine, service.confirm, identifier, values, run['revision'], draft['adoptions'])
        if ok:
            close_dialog()
            if updated['inputs']['path'] == 'trend' and not updated['research_id']:
                launch(engine, 'demand_search', identifier)
            st.rerun()
    if len(clarification_rounds(run)) < MAX_CLARIFICATION_ROUNDS and st.button('保存摘要并重新分析'):
        ok, _ = action(engine, service.update, identifier, values, service.draft_answers(run))
        if ok:
            close_dialog()
            analyze_dialog(engine, identifier)
    if can_answer(run, analysis) and st.button('返回补充问题'):
        close_dialog()
        open_dialog(identifier, 'questions')


def demand_page(engine):
    from lab.workbench_ui import header
    service = engine.workbench.demands
    work = engine.workbench
    busy = work.active is not None
    configured = work.llm.configured
    header('从生产能力，找到值得设计的产品', '明确需求、比较方向，把文化主题整理成设计方能使用的需求单。', 'ATLAS / PRODUCT EXPLORATION')
    st.caption('商家能力 → 需求澄清 → 准备依据 → 产品候选 → 设计需求单 → 设计方案（含图稿）')
    if not configured:
        st.info('可以先保存需求；分析与生成前请在模型设置中配置连接。')
        st.link_button('打开模型设置', '/?view=settings')
    runs = service.runs()
    names = {r['id']: f"{local_time(r['created_at'])} · {r['inputs']['request'][:30]} · {r['id'][:6]}" for r in runs}
    if st.session_state.pop('demand-clear-new', False):
        for key in list(st.session_state):
            if key.startswith('demand-new-') or key == 'demand-copy-source':
                del st.session_state[key]
    with st.expander('新建产品需求任务', expanded=not runs):
        copied = st.session_state.get('demand-new-merchant', {})
        if runs:
            copy_id = st.selectbox('从历史任务复制商家基础信息', [None, *names], format_func=lambda x: names.get(x, '不复制，独立填写'), key='demand-copy-source', disabled=busy)
            if st.button('复制所选商家信息', disabled=busy or not copy_id):
                for key, value in service.merchant(copy_id).items():
                    st.session_state['demand-new-' + key] = value
                st.session_state['demand-new-merchant'] = service.merchant(copy_id)
                st.rerun()
        with st.form('demand-create'):
            values = input_form(copied, 'demand-new-', busy)
            submitted = st.form_submit_button('保存新任务', type='primary', disabled=busy)
        if submitted:
            ok, created = action(engine, service.create, values)
            if ok:
                st.query_params['demand'] = created['id']
                st.session_state['demand-history'] = created['id']
                st.session_state['demand-clear-new'] = True
                st.rerun()
    if not runs:
        return
    requested = st.query_params.get('demand')
    if requested in names and st.session_state.get('demand-url-seen') != requested:
        st.session_state['demand-history'] = requested
        st.session_state['demand-url-seen'] = requested
    if st.session_state.get('demand-history') not in names:
        st.session_state['demand-history'] = runs[0]['id']
    identifier = st.selectbox('历史需求任务', list(names), format_func=names.get, key='demand-history')
    run = service.load(identifier)
    operations = list(run['operations'].values())
    op = operations[-1] if operations else None
    if op:
        fixed_status(engine, identifier, op['id'], op['status'] == 'running')
    prefix = f"demand-{identifier}-r{run['revision']}-"
    st.subheader('1 · 需求摘要与澄清', anchor='demand-clarify')
    analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
    st.caption('当前阶段：' + ('需求已确认' if run['approved_revision'] == run['revision'] else '完善需求中'))
    for field, label in FIELDS.items():
        st.write(f"{label}：{run['inputs'].get(field) or '未提供'}")
    if analysis:
        st.write('模型理解（建议）：' + '；'.join(analysis['known']))
        st.write('待探索：' + ('；'.join(analysis['unknown']) or '暂无'))
    st.caption('补充关键信息后，可随时确认需求并继续。')
    limited = len(clarification_rounds(run)) >= MAX_CLARIFICATION_ROUNDS and not can_answer(run, analysis)
    a, b = st.columns(2)
    if a.button('查看并确认需求' if limited else '继续完善需求' if analysis else '开始完善需求', disabled=busy or not configured):
        if analysis or limited:
            open_dialog(identifier, 'questions' if can_answer(run, analysis) else 'confirm')
        else:
            analyze_dialog(engine, identifier)
    if b.button('按当前需求继续', disabled=busy):
        open_dialog(identifier, 'confirm')
    step_feedback(engine, identifier, op, ('demand_analyze',), 'clarify')
    pending = st.session_state.get('demand-await')
    if pending and pending['id'] == identifier:
        awaited = run['operations'].get(pending['op'])
        if awaited and awaited['status'] != 'running' and not busy:
            st.session_state.pop('demand-await', None)
            if awaited['status'] == 'success':
                st.session_state['demand-dialog'] = {'id': identifier, 'stage': 'questions' if can_answer(run, analysis) else 'confirm'}
    dialog = st.session_state.get('demand-dialog')
    if dialog and dialog['id'] != identifier:
        close_dialog()
    elif dialog and (not busy or dialog['stage'] == 'waiting'):
        clarification_dialog(engine, identifier, dialog['stage'])
    approved = run['approved_revision'] == run['revision']
    st.subheader('2 · 准备依据', anchor='demand-research')
    if not approved:
        st.info('请先保存并按当前需求继续；历史成果仍可在下方查看。')
    research = next((r for r in run['research'] if r['id'] == run['research_id']), None)
    if run['inputs']['path'] == 'trend':
        st.caption('公开搜索发现近期话题；时间已核验不等于热度已验证。')
        if approved and st.button('重新搜索话题', disabled=busy or not configured):
            launch(engine, 'demand_search', identifier)
        step_feedback(engine, identifier, op, ('demand_search',), 'research')
        if research:
            for topic in research['topics']:
                with st.container(border=True):
                    st.write('**' + topic['title'] + '**')
                    st.caption(('时间已核验' if topic['date_status'] == 'verified' else '非已核实近期话题，仅作为文化参考') + ' · 热度未验证')
                    st.write(topic['fit'])
                    with st.expander('话题来源与日期依据'):
                        for sid in topic['source_ids']:
                            date = research['extractions'][sid]['date']
                            st.caption(f"[{sid}] 发布时间：{local_time(date.get('published_at'), date.get('published_precision'))} · 事件时间：{local_time(date.get('event_at'), date.get('event_precision'))}")
                            st.write(date['reason'])
                            for item in date['evidence']:
                                st.text(item['quote'])
                        evidence_view({s['id']: s for s in research['sources'] if s['id'] in topic['source_ids']})
                    if st.button('已选择' if topic['id'] == run['topic_id'] else '选择此话题', key=prefix + research['id'] + topic['id'], disabled=busy or topic['id'] == run['topic_id']):
                        ok, _ = action(engine, service.select_topic, identifier, topic['id'])
                        if ok:
                            st.rerun()
            for issue in research['issues']:
                st.caption(issue)
    else:
        st.caption('根据人群、场景和文化主题检索本地文本，不执行热点搜索。点击生成候选时准备资料。')
    st.subheader('3 · 比较产品候选', anchor='demand-candidates')
    can_generate = approved and (run['inputs']['path'] == 'custom' or bool(run['topic_id']))
    with st.form(prefix + 'generate'):
        count = st.selectbox('候选数量', [3, 5], disabled=busy)
        instruction = st.text_area('本轮候选调整意见（选填）', max_chars=4000, disabled=busy)
        generate = st.form_submit_button('生成新一轮产品候选', disabled=busy or not configured or not can_generate)
    if generate:
        launch(engine, 'demand_candidates', identifier, count=count, instruction=instruction)
    step_feedback(engine, identifier, op, ('demand_candidates',), 'candidates')
    if run['candidate_rounds']:
        batches = {b['id']: b for b in reversed(run['candidate_rounds'])}
        batch_id = st.selectbox('候选轮次', list(batches), format_func=lambda k: f"{local_time(batches[k]['at'])} · {k[:6]}" + (' · 历史条件' if stale(run, batches[k]) else ''), key=prefix + 'round-' + str(len(batches)))
        batch = batches[batch_id]
        historical = stale(run, batch)
        if historical:
            st.warning('这轮候选与当前条件不一致，可查看，不能直接用于当前设计需求单。')
        if batch['context']['knowledge_missing']:
            st.info('未检索到相关本地知识。产品方向为策划建议，不代表获得专业知识库支持。')
        if batch.get('format_version', 1) >= 3:
            st.write('本轮方案对比')
            st.table(candidate_comparison(batch))
            st.caption('选择表示认可产品方向；容量、材料和工艺等细节仍按建议及后续确认处理。')
        labels = {'form': '产品形态', 'audience': '目标人群', 'scene': '使用场景', 'theme': '文化主题', 'reason': '需求与推荐理由', 'capability_fit': '能力匹配判断', 'design': '差异化设计建议'}
        for candidate in batch['candidates']:
            with st.container(border=True):
                st.write('**' + candidate['name'] + '**')
                for field, label in labels.items():
                    st.write(f"{label}：{candidate[field]}")
                if batch.get('format_version', 1) >= 3:
                    st.write('核心用途：' + candidate['purpose'])
                    st.write('主要区别：' + candidate['differentiator'])
                    st.write('需确认能力：' + ('；'.join(candidate['capability_gaps']) or '未列出额外能力缺口，方案细节仍需验证'))
                if batch.get('format_version', 1) >= 2:
                    st.write('文化故事：' + candidate['story'])
                    st.write('视觉元素：' + candidate['visual_elements'])
                if batch.get('format_version', 1) >= 3:
                    with st.expander('故事到设计的对应（策划建议）'):
                        st.markdown(mapping_markdown(candidate['story_mapping']))
                with st.expander('必要假设与关键未知'):
                    st.caption('必要假设：' + ('；'.join(candidate['assumptions']) or '暂无'))
                    st.caption('关键未知：' + ('；'.join(candidate['questions']) or '暂无'))
                facts_view(candidate['facts'])
                selected = run['selection'] == {'round_id': batch_id, 'candidate_id': candidate['id']}
                if st.button('已选为设计方向' if selected else '选为设计方向', key=batch_id + candidate['id'], disabled=busy or historical or selected):
                    ok, _ = action(engine, service.select_candidate, identifier, batch_id, candidate['id'])
                    if ok:
                        st.rerun()
        with st.expander('本轮参考资料'):
            evidence_view(batch['context']['evidence'])
    st.subheader('4 · 设计需求单', anchor='demand-brief')
    if run['selection']:
        st.caption(f"当前选择：{run['selection']['round_id'][:6]} / {run['selection']['candidate_id']}")
    if st.button('生成设计需求单', type='primary', disabled=busy or not configured or not run['selection'] or not approved):
        launch(engine, 'demand_brief', identifier)
    step_feedback(engine, identifier, op, ('demand_brief',), 'brief')
    selected_brief = None
    if run['briefs']:
        briefs = {b['id']: b for b in reversed(run['briefs'])}
        bid = st.selectbox('需求单版本', list(briefs), format_func=lambda k: f"版本 {briefs[k]['version']} · {briefs[k]['content']['title']}", key=prefix + 'brief-' + str(len(briefs)))
        brief = briefs[bid]
        selected_brief = brief
        historical = stale(run, brief) or brief['selection'] != run['selection']
        if historical:
            st.warning('此需求单对应历史条件或其他产品选择；导出仍保留原始资料和版本。')
        if brief.get('format_version', 1) >= 2:
            for i, (title, body, auxiliary) in enumerate(brief_sections(run, brief)):
                if auxiliary:
                    with st.expander(title, expanded=False):
                        st.markdown(body)
                else:
                    st.markdown(('# ' if i == 0 else '## ') + title)
                    st.markdown(body)
        else:
            st.markdown(export_markdown(run, brief))
        a, b = st.columns(2)
        a.download_button('下载 Markdown', export_markdown(run, brief), file_name=f"design-brief-v{brief['version']}.md", mime='text/markdown')
        b.download_button('下载 JSON', json.dumps(export_bundle(run, brief), ensure_ascii=False, indent=2), file_name=f"design-brief-v{brief['version']}.json", mime='application/json')
        with st.expander('需求单原始参考资料'):
            evidence_view(brief['evidence'])
        st.subheader('修改需求单', anchor='demand-revise')
        with st.form(prefix + bid + '-revise'):
            revision = st.text_area('需求单修改意见', max_chars=4000, disabled=busy)
            revise = st.form_submit_button('修订为新版本', disabled=busy or not configured or historical)
        if revise:
            launch(engine, 'demand_revise', identifier, brief_id=bid, instruction=revision)
        step_feedback(engine, identifier, op, ('demand_revise',), 'revise')
    design_step(engine, run, selected_brief, op)
    with st.expander('需求与执行历史'):
        for summary in reversed(run['summaries']):
            st.write(f"需求版本 {summary['revision']} · {local_time(summary['at'])}")
            for key, label in FIELDS.items():
                st.caption(f"{label}：{summary['inputs'].get(key) or '未提供'}")
            for question, answer in summary['answers'].items():
                st.write(question + '：' + answer)
        for item in reversed(operations):
            st.write(f"{local_time(item['created_at'])} · {item['stage']} · {STATES[item['status']]}")
            if item.get('message'):
                st.caption(item['message'])
            for response in item['responses']:
                if response.get('error'):
                    st.caption('输出检查未通过：' + response['error'])
    st.markdown('<div style="height:88px" aria-hidden="true"></div>', unsafe_allow_html=True)


def design_step(engine, run, source_brief, op):
    from lab import demand_design
    work = engine.workbench
    busy = work.active is not None
    identifier = run['id']
    st.subheader('5 · 设计方案', anchor='demand-design')
    st.caption('生成文字方案后，自动为第一个核心应用生成三版平面概念图稿；方案修订后也会生成对应的新图稿。')
    if not work.images.configured:
        st.info('尚未配置图片服务，本次只生成文字方案。配置后可在此补生成图稿。')
    valid_source = bool(source_brief and not stale(run, source_brief) and
                        source_brief['selection'] == run['selection'] and run['approved_revision'] == run['revision'])
    if source_brief:
        st.write(f"来源需求单：版本 {source_brief['version']} · {source_brief['content']['title']}")
        if not valid_source:
            st.info('此来源对应历史条件或其他产品方向；请在上方选择当前有效需求单后生成。')
    else:
        st.info('先选择并生成一份设计需求单，再生成完整设计方案。')
    if st.button('生成设计方案', type='primary', disabled=busy or not work.llm.configured or not valid_source):
        launch(engine, 'demand_design_generate', identifier, brief_id=source_brief['id'], include_images=work.images.configured)
    step_feedback(engine, identifier, op, demand_design.KINDS, 'design')
    versions = list(reversed(run.get('design_versions', [])))
    if not versions:
        return
    source_id = source_brief['id'] if source_brief else 'none'
    current = next((i for i, v in enumerate(versions) if v['brief_id'] == source_id), 0)
    by_id = {v['id']: v for v in versions}
    selected = st.selectbox('设计方案历史版本', list(by_id), index=current,
        format_func=lambda k: f"设计版本 {by_id[k]['version']} · 来源需求单版本 {by_id[k]['brief_snapshot']['version']} · {by_id[k]['content']['title']}" + (' · 其他来源' if by_id[k]['brief_id'] != source_id else ''),
        key=f"demand-design-history-{identifier}-{source_id}-{len(versions)}")
    design = by_id[selected]
    same_source = design['brief_id'] == source_id
    historical = stale(run, design) or design['selection'] != run['selection']
    if historical:
        st.warning('此设计方案对应历史需求条件或其他产品方向；可查看和导出，不能直接修订。')
    elif not same_source:
        st.info('这是其他来源需求单的方案；如需修订，请在上方选择其来源需求单版本。')
    content = demand_design.sections(run, design)
    st.subheader(content[0][0])
    st.caption(content[0][1])
    tabs = st.tabs(['整体概念', '视觉设计', '产品应用与交付'])
    for tab, (_, body, _) in zip(tabs, content[1:4]):
        with tab:
            st.markdown(body)
    for title, body, _ in content[4:]:
        with st.expander(title, expanded=False):
            st.markdown(body)
    prefix = f"demand-design-{identifier}-{design['id']}-"
    with st.form(prefix + 'revise'):
        instruction = st.text_area('设计方案修改意见', placeholder='例如：主色改为墨绿，保留故事，简化盒面构图。', max_chars=4000, disabled=busy)
        revise = st.form_submit_button('生成设计方案新版本', disabled=busy or not work.llm.configured or historical or not same_source or not valid_source)
    if revise:
        launch(engine, 'demand_design_revise', identifier, design_id=design['id'], instruction=instruction, include_images=work.images.configured)
    a, b = st.columns(2)
    a.download_button('下载设计方案 Markdown', demand_design.export_markdown(run, design),
        file_name=f"design-proposal-{identifier[:8]}-v{design['version']}.md", mime='text/markdown', key=prefix + 'md')
    b.download_button('下载设计方案 JSON', json.dumps(demand_design.export_bundle(run, design), ensure_ascii=False, indent=2),
        file_name=f"design-proposal-{identifier[:8]}-v{design['version']}.json", mime='application/json', key=prefix + 'json')
    image_step(engine, run, design, valid_source and same_source and not historical, op)


def image_step(engine, run, design, valid_source, op):
    from lab import demand_images
    from lab.image_service import image_sizes
    work = engine.workbench
    busy = work.active is not None
    enabled = valid_source and work.images.configured and not busy
    st.subheader('设计图稿', anchor='demand-images')
    st.caption('平面概念稿，供设计讨论；像素尺寸不代表生产尺寸。修改图稿不改写文字方案。')
    st.caption(f"来源：设计版本 {design['version']} · {design['content']['title']}")
    if not work.images.configured:
        st.info('请在模型设置中独立配置图片服务，文字功能仍可使用。')
        st.link_button('配置图片模型', '/?view=settings')
    apps = design['content']['applications']
    prefix = f"demand-image-{run['id']}-{design['id']}-"
    index = st.selectbox('图稿设计项', list(range(len(apps))),
        index=next((i for i, a in enumerate(apps) if a['priority'] == 'core'), 0),
        format_func=lambda i: apps[i]['name'] + ('（核心）' if apps[i]['priority'] == 'core' else '（可选）'), key=prefix + 'app')
    sizes = image_sizes(work.images.settings.provider)
    labels = dict(zip(sizes, ('方形', '横向', '纵向')))
    size = st.selectbox('图稿像素规格', sizes, format_func=lambda s: labels[s] + ' · ' + s.replace('x', '×'), key=prefix + work.images.settings.provider + '-size')
    if st.button('补充生成三版设计图稿', key=prefix + 'generate', type='primary', disabled=not enabled or not work.llm.configured):
        launch(engine, 'demand_image_generate', run['id'], design_id=design['id'], application_index=index, size=size)
    step_feedback(engine, run['id'], op, demand_images.KINDS, 'images')
    st.caption('失败后可补生成未完成图稿；服务端已生成但尚未保存时中断，重试可能再次调用并计费。')
    assets = [a for a in run.get('image_assets', []) if a['design_id'] == design['id'] and a['application_index'] == index]
    if not assets:
        return
    groups = list(dict.fromkeys(a['operation_id'] for a in reversed(assets)))
    group = st.selectbox('图稿批次与修订历史', groups,
        format_func=lambda g: next(f"图稿版本 {a['version']} 起 · {a['title']}" for a in assets if a['operation_id'] == g),
        key=prefix + str(index) + '-history-' + str(len(assets)))
    chosen = run.get('image_selections', {}).get(design['id'] + ':' + str(index))
    if chosen:
        selected_asset = next((a for a in assets if a['id'] == chosen), None)
        if selected_asset:
            st.caption(f"已选用：图稿版本 {selected_asset['version']} · {selected_asset['title']}")
    shown = [a for a in assets if a['operation_id'] == group]
    for col, asset in zip(st.columns(len(shown)), shown):
        with col:
            st.write(f"图稿版本 {asset['version']} · {asset['title']}")
            try:
                raw = demand_images.asset_path(work.demands, run['id'], asset['path']).read_bytes()
            except (OSError, ValueError):
                st.error('图片文件不可读取，请检查工作区备份。')
                continue
            st.image(raw, caption='概念图稿 · 可放大查看', width='stretch')
            st.download_button('下载 PNG', raw, file_name=f"concept-{asset['id']}.png", mime='image/png', key=asset['id'] + '-download')
            if st.button('已选用' if chosen == asset['id'] else '选用此图稿', disabled=not valid_source or busy, key=asset['id'] + '-select'):
                ok, _ = action(engine, demand_images.select, work.demands, run['id'], asset['id'])
                if ok:
                    st.rerun()
    if chosen and any(a['id'] == chosen for a in assets):
        with st.form(prefix + str(index) + '-edit'):
            instruction = st.text_area('图稿修改意见', placeholder='改为墨绿，减少装饰，保留主体。', max_chars=4000)
            submit = st.form_submit_button('基于选用图稿生成新版本', disabled=not enabled)
        if submit:
            launch(engine, 'demand_image_revise', run['id'], image_id=chosen, instruction=instruction)
