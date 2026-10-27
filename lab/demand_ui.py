"""A staged product exploration UI; ordinary editing never calls the model."""
import json
from copy import deepcopy
from datetime import date
from html import escape
import streamlit as st

from lab.demands import MAX_CLARIFICATION_ROUNDS, clarification_rounds, can_answer, clean_inputs, adoption_value, FIELDS, BRIEF_FIELDS, stale, export_bundle, export_markdown, brief_sections, candidate_comparison, mapping_markdown, structured_lines, topic_note, current_day, capacity_status, brief_topics, brief_topic_scores
from lab.ui_kit import badges, category_icon, swatches as kit_swatches
import re

from lab.demand_presets import STYLES, TEMPLATES, TEMPLATE_FIELDS
from lab import demand_scoring
from lab.campaign_ui import local_time

PATHS = {'trend': '结合近期热点', 'custom': '人群／场景定制'}
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


def input_form(values, prefix, disabled=False, path=None):
    """path fixes the task's workbench: the path and hotspot range fields are then hidden, so a task never switches workbench."""
    result = {}
    result['request'] = st.text_area('原始需求', value=values.get('request', ''), key=prefix + 'request', max_chars=4000, disabled=disabled)
    a, b = st.columns(2)
    result['industry'] = a.text_input('行业（必填）', value=values.get('industry', ''), key=prefix + 'industry', max_chars=4000, disabled=disabled)
    result['brand'] = b.text_input('品牌名称（选填）', value=values.get('brand', ''), key=prefix + 'brand', max_chars=4000, disabled=disabled)
    result['capabilities'] = st.text_area('生产能力（必填）', value=values.get('capabilities', ''), placeholder='例如：可生产真丝丝巾和家居服，支持数码印花。', key=prefix + 'capabilities', max_chars=4000, disabled=disabled)
    result['constraints'] = st.text_area('已知限制（选填）', value=values.get('constraints', ''), placeholder='材料、工艺、价格范围和不能做的产品。', key=prefix + 'constraints', max_chars=4000, disabled=disabled)
    if path is None:
        a, b = st.columns(2)
        result['path'] = a.selectbox('探索路径', list(PATHS), index=list(PATHS).index(values.get('path', 'trend')), format_func=PATHS.get, key=prefix + 'path', disabled=disabled)
        result['days'] = b.selectbox('热点搜索时间范围', [1, 7, 30], index=[1, 7, 30].index(values.get('days', 7)), format_func=lambda n: '最近 24 小时' if n == 1 else f'最近 {n} 天', key=prefix + 'days', disabled=disabled)
    else:
        result['path'], result['days'] = path, values.get('days', 7)
    templates = ['', *TEMPLATES]
    result['template'] = values.get('template', '') if result['path'] == 'trend' and path else st.selectbox('场景模板（选填）', templates, index=templates.index(values.get('template', '')),
        format_func=lambda k: TEMPLATES[k]['label'] if k else '不使用模板', key=prefix + 'template', disabled=disabled,
        on_change=apply_template, args=(prefix,), help='选择后切换到人群／场景定制，并预填尚未填写的人群、场景、用途和主题草稿；模板关注点会提供给澄清和候选生成。')
    custom_columns = st.columns(2) if result['path'] == 'custom' else None
    for i, key in enumerate(('audience', 'scene', 'purpose', 'theme')):
        result[key] = (custom_columns[i % 2].text_input(FIELDS[key], value=values.get(key, ''), key=prefix + key, max_chars=4000, disabled=disabled)
                       if result['path'] == 'custom' else values.get(key, ''))
    if result['path'] == 'custom':
        current = values.get('use_date')
        picked = st.date_input('使用日期（选填）', value=date.fromisoformat(current) if current else None, key=prefix + 'use-date',
                               disabled=disabled, help='如婚期、校庆日或交付日期，用于倒排生产与设计交付。')
        result['use_date'] = picked.isoformat() if picked else ''
        st.caption('定制路径至少填写目标人群、使用场景或文化主题中的一项。品牌未填写时保留为“未提供”。')
    else:
        result['use_date'] = values.get('use_date', '')
    if result['template']:
        st.caption('模板关注点：' + TEMPLATES[result['template']]['focus'])
    result['capacity'], result['budget'] = capacity_form(values, prefix, disabled)
    return result


def apply_template(prefix):
    # Runs before the rerun, so widget values can still be set. Only empty fields are prefilled.
    template = st.session_state.get(prefix + 'template')
    if not template:
        return
    st.session_state[prefix + 'path'] = 'custom'
    for field in TEMPLATE_FIELDS:
        if not st.session_state.get(prefix + field):
            st.session_state[prefix + field] = TEMPLATES[template][field]


def split_items(text):
    """Comma, 顿号, semicolon or line separated items; duplicates dropped, order kept."""
    return list(dict.fromkeys(x.strip() for x in re.split(r'[,，、;；\n]+', text or '') if x.strip()))


def capacity_form(values, prefix, disabled, wrap=True, budget=True):
    """Categories and crafts are typed by the merchant, so any industry fits; labels stay short enough for one line."""
    cap, bud = values.get('capacity') or {}, values.get('budget') or {}
    with st.expander('产能与预算（选填）', expanded=bool(cap or bud)) if wrap else st.container():
        with st.container(key=prefix + 'capacity-box'):
            st.caption('填写后，产品候选按可生产品类、工艺和产能做硬性检查，需求单的定价参考需与预算单价有重叠。数字填 0 表示不限。')
            a, b = st.columns(2)
            categories = a.text_input('可生产品类', '、'.join(cap.get('categories', [])), max_chars=600, key=prefix + 'cap-categories',
                                      placeholder='多项用顿号或逗号分隔', disabled=disabled,
                                      help='填写企业能生产的产品品类，例如：瓶装黄酒、礼盒装、定制酒标。候选方向的品类必须在其中。')
            crafts = b.text_input('可用工艺', '、'.join(cap.get('crafts', [])), max_chars=600, key=prefix + 'cap-crafts',
                                  placeholder='多项用顿号或逗号分隔', disabled=disabled,
                                  help='填写企业已具备的工艺，例如：手工冬酿、陶坛陈酿、丝网印刷。候选需要的其他工艺会标为需新增能力。')
            c, d, e = st.columns(3)
            lead = c.number_input('交期（天）', 0, 365, cap.get('lead_days', 0), key=prefix + 'cap-lead', disabled=disabled, help='常规生产交期。')
            monthly = d.number_input('月产能（件）', 0, 10_000_000, cap.get('monthly', 0), step=100, key=prefix + 'cap-monthly', disabled=disabled)
            moq = e.number_input('起订量（件）', 0, 10_000_000, cap.get('moq', 0), step=50, key=prefix + 'cap-moq', disabled=disabled)
            capacity = {'categories': split_items(categories), 'crafts': split_items(crafts), 'lead_days': int(lead), 'monthly': int(monthly), 'moq': int(moq)}
            if not budget:
                return capacity, {}
            f, g, h = st.columns(3)
            low = f.number_input('最低单价（元）', 0.0, 1_000_000.0, float(bud.get('price_min', 0)), step=10.0, key=prefix + 'budget-min',
                                 disabled=disabled, help='预算单价下限，元／件。')
            high = g.number_input('最高单价（元）', 0.0, 1_000_000.0, float(bud.get('price_max', 0)), step=10.0, key=prefix + 'budget-max',
                                  disabled=disabled, help='预算单价上限，元／件。')
            quantity = h.number_input('数量（件）', 0, 10_000_000, bud.get('quantity', 0), step=50, key=prefix + 'budget-quantity',
                                      disabled=disabled, help='预算对应的采购数量。')
    return capacity, {'price_min': low, 'price_max': high, 'quantity': int(quantity)}


def element_widgets(refs, key, disabled):
    def label(ref):
        return f"{ref} · {refs[ref]['label']}"
    if refs:
        a, b = st.columns(2)
        adopt = a.multiselect('采用文化素材', list(refs), format_func=label, key=key + '-adopt', disabled=disabled)
        remove = b.multiselect('移除文化素材', list(refs), format_func=label, key=key + '-remove', disabled=disabled)
    else:
        adopt, remove = [], []
        st.caption('本轮没有文化知识库素材可调整。')
    style = st.selectbox('风格预设（选填）', ['', *STYLES], format_func=lambda x: x or '不调整', key=key + '-style', disabled=disabled)
    return {'adopt': adopt, 'remove': remove, 'style': style}


swatches = kit_swatches


def topics_step(engine, run, research, prefix, busy):
    service = engine.workbench.demands
    identifier = run['id']
    topics = service.topics(run)
    kinds = {'search': '搜索话题', 'calendar': '日历节点', 'manual': '手动热点'}
    st.caption('话题按预测评分排序，由时效、节点临近、能力关联和人工热度按权重合成；' + demand_scoring.NOTE + '。不适用的分量不计入；为任一话题填写人工热度后，未填写的话题按 0 计。')
    if not (run['inputs'].get('capacity') or {}).get('lead_days'):
        st.caption(f'未设置常规交期，节点临近按 {demand_scoring.DEFAULT_LEAD} 天交期估算。')
    for topic in topics:
        score = topic['score']
        with st.container(border=True):
            st.write('**' + topic['title'] + '**' + (f"　{topic['date'][:10]}" if topic['kind'] != 'search' and topic.get('date') else ''))
            value = '未评分' if score['score'] is None else f"{score['score']:.2f}"
            parts = ' · '.join(f"{demand_scoring.LABELS[k]} {v:.2f}" for k, v in score['components'].items())
            st.caption(f"{kinds[topic['kind']]} · 预测评分 {value}" + (' · ' + parts if parts else '') + ''.join(' · ' + f for f in score['flags']))
            st.caption(topic_note(topic))
            st.write(topic['fit'])
            if topic['kind'] == 'search':
                with st.expander('话题来源与日期依据'):
                    for sid in topic['source_ids']:
                        date = research['extractions'][sid]['date']
                        st.caption(f"[{sid}] 发布时间：{local_time(date.get('published_at'), date.get('published_precision'))} · 事件时间：{local_time(date.get('event_at'), date.get('event_precision'))}")
                        st.write(date['reason'])
                        for item in date['evidence']:
                            st.text(item['quote'])
                    evidence_view({s['id']: s for s in research['sources'] if s['id'] in topic['source_ids']})
            elif topic.get('url'):
                st.caption('参考链接（只记录，不读取）：' + topic['url'])
            if st.button('已选择' if topic['id'] == run['topic_id'] else '选择此话题', key=prefix + 'topic-' + topic['id'], disabled=busy or topic['id'] == run['topic_id']):
                ok, _ = action(engine, service.select_topic, identifier, topic['id'])
                if ok:
                    st.rerun()
    if research:
        for issue in research['issues']:
            st.caption(issue)
    with st.expander('添加手动热点'):
        manual_topic_form(engine, run, prefix, busy)
    with st.expander('评分权重与人工热度'):
        scoring_form(engine, run, topics, prefix, busy)


TOPIC_KINDS = {'search': '搜索话题', 'calendar': '日历节点', 'manual': '手动热点'}


def manual_topic_form(engine, run, prefix, busy):
    with st.form(prefix + 'manual-topic'):
        title = st.text_input('热点标题', max_chars=60)
        note = st.text_area('说明（选填）', max_chars=500)
        url = st.text_input('参考链接（选填，只记录不读取）', max_chars=1000)
        day = st.date_input('节点日期（选填）', value=None)
        add = st.form_submit_button('添加热点', disabled=busy)
    if add:
        ok, _ = action(engine, engine.workbench.demands.add_manual_topic, run['id'], title, note, url, day.isoformat() if day else None)
        if ok:
            st.rerun()


def scoring_form(engine, run, topics, prefix, busy):
    scoring = run.get('scoring', {})
    weights = {**demand_scoring.WEIGHTS, **scoring.get('weights', {})}
    heat = scoring.get('heat', {})
    with st.form(prefix + 'scoring'):
        columns = st.columns(4)
        chosen = {k: columns[i].slider(demand_scoring.LABELS[k], 0.0, 1.0, float(weights[k]), 0.05) for i, k in enumerate(demand_scoring.WEIGHTS)}
        st.caption('人工热度为 1–5，0 表示未填写。调整只重新排序，不调用模型，也不生成新版本。')
        entered = {t['id']: st.number_input(f"{TOPIC_KINDS[t['kind']]}：{t['title']}", 0, 5, heat.get(t['id'], 0), key=prefix + 'heat-' + t['id']) for t in topics}
        save = st.form_submit_button('保存并重新排序', disabled=busy)
    if save:
        merged = {**{k: v for k, v in heat.items() if k not in entered}, **{k: int(v) for k, v in entered.items() if v}}
        ok, _ = action(engine, engine.workbench.demands.set_scoring, run['id'], chosen, merged)
        if ok:
            st.rerun()


def calendar_editor(engine, prefix='settings-calendar-'):
    service = engine.workbench.demands
    busy = engine.workbench.active is not None
    st.caption(f'未来 {demand_scoring.WINDOW_DAYS} 天内的节点会出现在爆款预测的话题中。修改日历不影响已选中话题的快照。')
    today = current_day().isoformat()
    nodes = [n for n in service.calendar.nodes() if n['date'] >= today]
    st.dataframe([{'日期': n['date'], '名称': n['name'], '类型': n['kind'], '说明': n['note'], '来源': '内置' if n['origin'] == 'builtin' else '自定义'} for n in nodes],
                 hide_index=True, width='stretch')
    with st.form(prefix + 'add'):
        a, b, c = st.columns(3)
        name = a.text_input('节点名称', max_chars=30)
        day = b.date_input('节点日期', value=None)
        kind = c.selectbox('类型', list(demand_scoring.KINDS))
        note = st.text_input('说明（选填）', max_chars=200)
        add = st.form_submit_button('添加节点', disabled=busy)
    if add:
        ok, _ = action(engine, service.calendar.add, name, day.isoformat() if day else '', kind, note)
        if ok:
            st.rerun()
    labels = {n['key']: f"{n['date']} · {n['name']}" for n in nodes}
    target = st.selectbox('删除节点', [None, *labels], format_func=lambda k: labels.get(k, '选择要删除的节点'), key=prefix + 'remove')
    if st.button('删除所选节点', disabled=busy or not target, key=prefix + 'remove-button'):
        ok, _ = action(engine, service.calendar.remove, target)
        if ok:
            st.rerun()


def enterprise_picker(engine, key, disabled, label='选择企业档案'):
    """Returns (profile id, task fields). The profile only prefills; tasks keep their own copy."""
    from lab.enterprises import Enterprises
    store = engine.workbench.enterprises
    profiles = store.list()
    if not profiles:
        st.caption('尚未保存企业档案，可在「系统 → 模型、企业与日历设置」中添加，或在任务中另存。')
        return None, {}
    names = {p['id']: p['name'] for p in profiles}
    chosen = st.selectbox(label, [None, *names], format_func=lambda k: names.get(k, '不使用档案'), key=key, disabled=disabled)
    profile = store.get(chosen) if chosen else None
    return (chosen, Enterprises.task_fields(profile)) if profile else (None, {})


def task_select(engine, path, label):
    """Task picker for one workbench; follows ?demand= so links and job panels open the right task."""
    service = engine.workbench.demands
    runs = [r for r in service.runs() if r['inputs']['path'] == path]
    if not runs:
        return None
    names = {r['id']: f"{local_time(r['created_at'])} · {r['inputs']['request'][:28]} · {r['id'][:6]}" for r in runs}
    key = path + '-task'
    requested = st.query_params.get('demand')
    if requested in names and st.session_state.get(key + '-url') != requested:
        st.session_state[key] = requested
        st.session_state[key + '-url'] = requested
    if st.session_state.get(key) not in names:
        st.session_state[key] = runs[0]['id']

    def changed():
        st.query_params['demand'] = st.session_state[key]
        st.session_state[key + '-url'] = st.session_state[key]
    identifier = st.selectbox(label, list(names), format_func=names.get, key=key, on_change=changed)
    return service.load(identifier)


CANDIDATE_LABELS = {'form': '产品形态', 'audience': '目标人群', 'scene': '使用场景', 'theme': '文化主题', 'reason': '需求与推荐理由', 'capability_fit': '能力匹配判断', 'design': '差异化设计建议'}


def candidate_detail(batch, candidate):
    for field, label in CANDIDATE_LABELS.items():
        st.write(f"{label}：{candidate[field]}")
    if batch.get('format_version', 1) >= 3:
        st.write('核心用途：' + candidate['purpose'])
        st.write('主要区别：' + candidate['differentiator'])
        st.write('需确认能力：' + ('；'.join(candidate['capability_gaps']) or '未列出额外能力缺口，方案细节仍需验证'))
    if batch.get('format_version', 1) >= 4:
        req = candidate['requirements']
        st.write('品类与工艺：' + req['category'] + ('；' + '、'.join(req['crafts']) if req['crafts'] else ''))
        st.write(f"爆款潜力（模型判断）：{candidate['potential']['score']}/5 · {candidate['potential']['reason']}")
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


def candidate_cards(engine, run, batch, busy, after_select=None):
    """Sketch-style candidate cards: category icon, difference, model-judged potential and capacity check."""
    service = engine.workbench.demands
    historical = stale(run, batch)
    v4 = batch.get('format_version', 1) >= 4
    capacity = batch['context']['summary']['inputs'].get('capacity') or {}
    for candidate in batch['candidates']:
        with st.container(border=True, key=f"candidate-{batch['id']}-{candidate['id']}"):
            category = candidate['requirements']['category'] if v4 else candidate['form']
            number = candidate['id'].removeprefix('C')
            st.markdown(f"**{category_icon(category)} 候选 {number} · {escape(candidate['name'])}**")
            st.write(candidate.get('differentiator') or candidate['design'])
            if v4:
                req = candidate['requirements']
                st.caption(req['category'] + (' · ' + '、'.join(req['crafts']) if req['crafts'] else ''))
                st.progress(candidate['potential']['score'] / 5, text=f"爆款潜力 {candidate['potential']['score']}/5")
                status = capacity_status(candidate, capacity)
                st.markdown(badges('model', 'unset' if not capacity else 'gap' if '需新增' in status else 'pass'))
                if '需新增' in status:
                    st.caption(status)
            with st.expander('详情与依据'):
                candidate_detail(batch, candidate)
            selected = run['selection'] == {'round_id': batch['id'], 'candidate_id': candidate['id']}
            if st.button('已选为方向' if selected else '选为方向', key=f"pick-{batch['id']}-{candidate['id']}",
                         disabled=busy or historical or selected, type='primary' if selected else 'secondary', width='stretch'):
                ok, _ = action(engine, service.select_candidate, run['id'], batch['id'], candidate['id'])
                if ok:
                    if after_select:
                        after_select()
                    st.rerun()


def round_topics_caption(batch):
    context = batch['context']
    scores = brief_topic_scores(context)
    for topic in brief_topics(context):
        score = (scores.get(topic['id']) or {}).get('score')
        st.caption(f"选题：{topic['title']}" + (f" · 预测评分 {score:.2f}" if score is not None else '') + ' · ' + topic_note(topic))


def history_view(run):
    for summary in reversed(run['summaries']):
        st.write(f"需求版本 {summary['revision']} · {local_time(summary['at'])}")
        for key, label in FIELDS.items():
            st.caption(f"{label}：{summary['inputs'].get(key) or '未提供'}")
        for line in structured_lines(summary['inputs']):
            st.caption(line)
        for question, answer in summary['answers'].items():
            st.write(question + '：' + answer)
    for item in reversed(list(run['operations'].values())):
        st.write(f"{local_time(item['created_at'])} · {item['stage']} · {STATES[item['status']]}")
        if item.get('message'):
            st.caption(item['message'])
        for response in item['responses']:
            if response.get('error'):
                st.caption('输出检查未通过：' + response['error'])


def evidence_view(evidence):
    if not evidence:
        st.caption('没有可引用的资料，以下方向属于策划建议。')
    for ref, item in evidence.items():
        with st.expander(f"[{ref}] {item.get('title') or item.get('file_path') or '知识库片段'}"):
            if item.get('url'):
                st.link_button('打开来源', item['url'])
            if item.get('card'):
                st.caption('文化素材摘要（' + ('设计解读，不作为史实' if item['basis'] == 'interpretation' else '资料记载') + '）')
                st.text(item['card'])
                st.caption('原文引文')
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
    'demand_pipeline': ('demand-design', '纹样任务'),
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


def inline_questions(engine, run, analysis, await_key=None):
    """Clarification questions in the page instead of a dialog; submitting a round starts the next analysis."""
    service = engine.workbench.demands
    busy = engine.workbench.active is not None
    ok, session = action(engine, service.clarification_session, run['id'], analysis['id'])
    if not ok:
        return
    index = session['cursor']
    q = session['questions'][index]
    saved = session['answers'].get(q['id'], {})
    key = f"inline-q-{session['id']}-{q['id']}-"
    st.caption(f"第 {session['round']} 轮追问 · 第 {index + 1} / {len(session['questions'])} 题")
    st.markdown('**' + escape(q['text']) + '**')
    choice = st.pills('可选回答', q['options'], default=saved.get('choice') or None, key=key + 'choice', disabled=busy) if q['options'] else None
    text = st.text_area('补充说明／其他回答', saved.get('text', ''), max_chars=4000, key=key + 'text', disabled=busy, height=90)
    unknown = st.checkbox('暂不确定，请给建议', saved.get('unknown', False), key=key + 'unknown', disabled=busy)
    a, b = st.columns(2)
    last = index == len(session['questions']) - 1
    if a.button('上一题', key=key + 'back', disabled=busy or index == 0, width='stretch'):
        if choice or text.strip() or unknown:
            ok, _ = action(engine, service.save_answer, run['id'], session['id'], index, choice or '', text, unknown, index - 1)
        else:
            ok, _ = action(engine, service.move_question, run['id'], session['id'], index - 1)
        if ok:
            st.rerun()
    if b.button('提交本轮回答' if last else '保存并下一题', key=key + 'next', type='primary', disabled=busy, width='stretch'):
        ok, _ = action(engine, service.save_answer, run['id'], session['id'], index, choice or '', text, unknown, index + 1)
        if ok and last:
            ok, result = action(engine, engine.workbench.start, 'demand_analyze', {'run_id': run['id'], 'session_id': session['id']})
            if ok and await_key:
                st.session_state[await_key] = {'id': run['id'], 'op': result[1].value['operation_id']}
        if ok:
            st.rerun()


def design_cards(engine, run, brief, busy, prefix='custom', heading='方案确认区'):
    from lab import demand_images
    service = engine.workbench.demands
    if heading:
        st.subheader(heading)
    if not brief:
        st.caption('先在「方向与元素」中生成设计说明。')
        return
    versions = [v for v in run.get('design_versions', []) if v['brief_id'] == brief['id'] and not stale(run, v) and v['selection'] == run['selection']]
    if not versions:
        st.caption('在下方生成设计方案，默认一次生成三版供比较。')
        return
    latest = versions[-1]
    shown = [v for v in versions if latest.get('group_id') and v.get('group_id') == latest['group_id']] or versions[-3:]
    for column, design in zip(st.columns(len(shown)), shown):
        with column, st.container(border=True, key=f'{prefix}-design-' + design['id']):
            content = design['content']
            label = f"方案 {design['variant']}" if design.get('variant') else f"设计版本 {design['version']}"
            st.markdown(f"**{label} · {escape(content['title'])}**")
            asset = next((a for a in run.get('image_assets', []) if a['design_id'] == design['id']), None)
            if asset:
                try:
                    st.image(demand_images.asset_path(service, run['id'], asset['path']).read_bytes(), caption=asset['title'] + '（概念图稿）', width='stretch')
                except (OSError, ValueError):
                    st.caption('图稿文件不可读取。')
            st.markdown(swatches(content['palette']), unsafe_allow_html=True)
            st.caption(content['concept'][:90])
            st.caption('核心应用：' + '、'.join(a['name'] for a in content['applications'] if a['priority'] == 'core'))
            chosen = run.get('design_choice') == design['id']
            if st.button('已确认' if chosen else '确认此方案', key=f'{prefix}-choose-' + design['id'], disabled=busy or chosen,
                         type='primary' if chosen else 'secondary', width='stretch'):
                if action(engine, service.choose_design, run['id'], design['id'])[0]:
                    st.rerun()


def adopt_to_draft(prefix, analysis, kind, field):
    draft = st.session_state[prefix + 'draft']
    if draft['invalidated']:
        return
    value = adoption_value(analysis, kind, field)
    st.session_state[prefix + field] = value
    draft['adoptions'] = [a for a in draft['adoptions'] if a['field'] != field]
    draft['adoptions'].append({'analysis_id': analysis['id'], 'kind': kind, 'field': field})
    draft['expected'][field] = value


def confirmation_view(engine, run, analysis, inline=False, auto_search=True):
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
    if inline:
        with st.expander('编辑需求摘要'):
            values = input_form(run['inputs'], prefix, path=run['inputs']['path'])
    else:
        st.write('可编辑需求摘要')
        values = input_form(run['inputs'], prefix, path=run['inputs']['path'])
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
    if st.button('确认需求并继续', type='primary', key=prefix + 'confirm'):
        ok, updated = action(engine, service.confirm, identifier, values, run['revision'], draft['adoptions'])
        if ok:
            close_dialog()
            if auto_search and updated['inputs']['path'] == 'trend' and not updated['research_id']:
                launch(engine, 'demand_search', identifier)
            st.rerun()
    if len(clarification_rounds(run)) < MAX_CLARIFICATION_ROUNDS and st.button('保存摘要并重新分析', key=prefix + 'reanalyze'):
        ok, _ = action(engine, service.update, identifier, values, service.draft_answers(run))
        if ok:
            close_dialog()
            if inline:
                launch(engine, 'demand_analyze', identifier)
            analyze_dialog(engine, identifier)
    if not inline and can_answer(run, analysis) and st.button('返回补充问题'):
        close_dialog()
        open_dialog(identifier, 'questions')


def dialog_host(engine, run, busy, auto_approve=False):
    """Shows the clarification dialog for this task when one is open or an awaited analysis finishes.
    With auto_approve, an analysis without open questions confirms the requirement instead of opening the dialog."""
    identifier = run['id']
    analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
    pending = st.session_state.get('demand-await')
    if pending and pending['id'] == identifier:
        awaited = run['operations'].get(pending['op'])
        if awaited and awaited['status'] != 'running' and not busy:
            st.session_state.pop('demand-await', None)
            if awaited['status'] == 'success' and auto_approve and not can_answer(run, analysis):
                close_dialog()
                if action(engine, engine.workbench.demands.approve, identifier)[0]:
                    st.rerun()
            elif awaited['status'] == 'success':
                st.session_state['demand-dialog'] = {'id': identifier, 'stage': 'questions' if can_answer(run, analysis) else 'confirm'}
    dialog = st.session_state.get('demand-dialog')
    if dialog and dialog['id'] != identifier:
        close_dialog()
    elif dialog and (not busy or dialog['stage'] == 'waiting'):
        clarification_dialog(engine, identifier, dialog['stage'])


def demand_page(engine):
    from lab.workbench_ui import header
    service = engine.workbench.demands
    work = engine.workbench
    busy = work.active is not None
    configured = work.llm.configured
    header('从生产能力，找到值得设计的产品', '明确需求、比较方向，把文化主题整理成设计方能使用的需求单。', 'ATLAS / PRODUCT EXPLORATION')
    st.caption('从需求到图稿，在一个工作区完成探索、设计与交付。')
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
                for key in [k for k in st.session_state if k.startswith(('demand-new-cap-', 'demand-new-budget-'))]:
                    del st.session_state[key]
                for key, value in service.merchant(copy_id).items():
                    st.session_state['demand-new-' + key] = value
                st.session_state['demand-new-merchant'] = service.merchant(copy_id)
                st.rerun()
        values = input_form(copied, 'demand-new-', busy)
        submitted = st.button('保存新任务', type='primary', disabled=busy)
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
    steps = [('demand-clarify', '需求澄清', run['approved_revision'] == run['revision']),
             ('demand-research', '热点依据', bool(run['research_id'])),
             ('demand-candidates', '产品方向', bool(run['selection'])),
             ('demand-brief', '设计需求单', any(not stale(run, b) and b['selection'] == run['selection'] for b in run['briefs'])),
             ('demand-design', '方案与图稿', any(not stale(run, d) and d['selection'] == run['selection'] for d in run.get('design_versions', [])))]
    st.markdown('<nav class="design-steps" aria-label="设计流程导航">' + ''.join(
        f'<a href="#{anchor}" target="_self" class="{"complete" if done else ""}"><span>{i:02d}</span><strong>{label}</strong><small>{"已有成果" if done else "待完成"}</small></a>'
        for i, (anchor, label, done) in enumerate(steps, 1)) + '</nav>', unsafe_allow_html=True)
    st.subheader('1 · 需求摘要与澄清', anchor='demand-clarify')
    analysis = next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)
    st.caption('当前阶段：' + ('需求已确认' if run['approved_revision'] == run['revision'] else '完善需求中'))
    st.markdown('<div class="demand-summary"><div class="summary-request">' + escape(run['inputs'].get('request') or '尚未填写原始需求') + '</div><div class="summary-tags">' + ''.join(
        '<span>' + escape(label + ' · ' + (run['inputs'].get(key) or '未提供')) + '</span>'
        for key, label in [('industry', '行业'), ('brand', '品牌'), ('audience', '人群'), ('scene', '场景')]) + '</div></div>', unsafe_allow_html=True)
    with st.expander('完整需求与分析', expanded=not analysis):
        for field, label in FIELDS.items():
            st.write(f"{label}：{run['inputs'].get(field) or '未提供'}")
        for line in structured_lines(run['inputs']):
            st.write(line)
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
    dialog_host(engine, run, busy)
    approved = run['approved_revision'] == run['revision']
    st.subheader('2 · 准备依据', anchor='demand-research')
    if not approved:
        st.info('请先保存并按当前需求继续；历史成果仍可在下方查看。')
    research = next((r for r in run['research'] if r['id'] == run['research_id']), None)
    if run['inputs']['path'] == 'custom':
        st.caption('围绕目标人群、场景和用途搜索近期热点，并结合本地文化资料。未选择话题时，生成候选会使用首个推荐话题；无可用正文时提示重试。')
    st.caption('公开搜索发现近期话题；时间已核验不等于热度已验证。')
    if approved and st.button('重新搜索话题', disabled=busy or not configured):
        launch(engine, 'demand_search', identifier)
    step_feedback(engine, identifier, op, ('demand_search',), 'research')
    topics_step(engine, run, research, prefix, busy)
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
        if batch['context'].get('culture_missing') is True:
            st.info('文化知识库中没有匹配的纹样或配色素材，可在「文化知识库」解码相关资料后重新生成。')
        if batch.get('format_version', 1) >= 4:
            if not batch['context']['summary']['inputs'].get('capacity'):
                st.caption('未设置产能参数，本轮没有做产能硬性检查。')
            if batch.get('capacity_log'):
                with st.expander(f"产能检查记录 · {len(batch['capacity_log'])} 个方向已自动修正"):
                    for item in batch['capacity_log']:
                        st.write(item['name'] + '：' + '；'.join(item['problems']))
            score = batch['context'].get('topic_score') or {}
            if score.get('score') is not None:
                st.caption(f"所选话题预测评分 {score['score']:.2f}（{score['note']}）")
        if batch.get('format_version', 1) >= 3:
            st.write('本轮方案对比')
            st.table(candidate_comparison(batch))
            st.caption('选择表示认可产品方向；容量、材料和工艺等细节仍按建议及后续确认处理。')
        for candidate in batch['candidates']:
            with st.container(border=True):
                st.write('**' + candidate['name'] + '**')
                st.write(candidate.get('differentiator') or candidate['design'])
                with st.expander('查看产品详情与依据'):
                    candidate_detail(batch, candidate)
                selected = run['selection'] == {'round_id': batch_id, 'candidate_id': candidate['id']}
                if st.button('已选为设计方向' if selected else '选为设计方向', key=batch_id + candidate['id'], disabled=busy or historical or selected):
                    ok, _ = action(engine, service.select_candidate, identifier, batch_id, candidate['id'])
                    if ok:
                        st.rerun()
        with st.expander('本轮参考资料'):
            evidence_view(batch['context']['evidence'])
    selected_brief = brief_step(engine, run, prefix, op)
    design_step(engine, run, selected_brief, op)
    with st.expander('需求与执行历史'):
        history_view(run)
    st.markdown('<div style="height:88px" aria-hidden="true"></div>', unsafe_allow_html=True)


def brief_step(engine, run, prefix, op, heading='4 · 设计需求单', generate=True):
    work = engine.workbench
    busy = work.active is not None
    configured = work.llm.configured
    identifier = run['id']
    approved = run['approved_revision'] == run['revision']
    st.subheader(heading, anchor='demand-brief')
    if generate:
        if run['selection']:
            st.caption(f"当前选择：{run['selection']['round_id'][:6]} / {run['selection']['candidate_id']}")
        else:
            st.caption('请先选定一个产品方向。')
        if st.button('生成设计需求单', type='primary', disabled=busy or not configured or not run['selection'] or not approved, key=prefix + 'brief-generate'):
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
                if auxiliary or i > 1:
                    with st.expander(title, expanded=False):
                        st.markdown(body)
                else:
                    st.markdown(('### ' if i == 0 else '#### ') + title)
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
            changes = element_widgets({k: v for k, v in brief['evidence'].items() if 'element_id' in v}, prefix + bid, busy)
            revise = st.form_submit_button('修订为新版本', disabled=busy or not configured or historical)
        if revise:
            launch(engine, 'demand_revise', identifier, brief_id=bid, instruction=revision, element_changes=changes)
        step_feedback(engine, identifier, op, ('demand_revise',), 'revise')
    return selected_brief


def design_step(engine, run, source_brief, op, heading='5 · 设计方案', default_variants=1):
    from lab import demand_design
    work = engine.workbench
    busy = work.active is not None
    identifier = run['id']
    st.subheader(heading, anchor='demand-design')
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
    variants = st.selectbox('一次生成方案数', [1, 3], index=[1, 3].index(default_variants), format_func=lambda n: '1 版（含图稿）' if n == 1 else '3 版对比（仅文字）',
                            key=f'demand-design-variants-{identifier}', disabled=busy)
    if variants == 3:
        st.caption('三版方案在概念、构图或配色上相互区分，只生成文字；选定一版后再生成图稿。')
    if st.button('生成设计方案', type='primary', disabled=busy or not work.llm.configured or not valid_source):
        launch(engine, 'demand_design_generate', identifier, brief_id=source_brief['id'], include_images=work.images.configured and variants == 1, variants=variants)
    if st.button('一键生成设计方案与纹样初稿', disabled=busy or not work.llm.configured or not valid_source or not work.images.configured or variants != 1,
                 help='生成一版文字方案后，按配色、故事映射和方案包中的纹样方向生成三个可平铺纹样单元。'):
        launch(engine, 'demand_design_generate', identifier, brief_id=source_brief['id'], include_images=True, image_mode='pattern')
    step_feedback(engine, identifier, op, demand_design.KINDS, 'design')
    versions = list(reversed(run.get('design_versions', [])))
    if not versions:
        return
    source_id = source_brief['id'] if source_brief else 'none'
    choice = run.get('design_choice')
    current = next((i for i, v in enumerate(versions) if v['id'] == choice and v['brief_id'] == source_id),
                   next((i for i, v in enumerate(versions) if v['brief_id'] == source_id), 0))
    by_id = {v['id']: v for v in versions}
    selected = st.selectbox('设计方案历史版本', list(by_id), index=current,
        format_func=lambda k: f"设计版本 {by_id[k]['version']} · 来源需求单版本 {by_id[k]['brief_snapshot']['version']} · {by_id[k]['content']['title']}" + (' · 其他来源' if by_id[k]['brief_id'] != source_id else ''),
        key=f"demand-design-history-{identifier}-{source_id}-{len(versions)}-{choice or ''}")
    design = by_id[selected]
    same_source = design['brief_id'] == source_id
    historical = stale(run, design) or design['selection'] != run['selection']
    if historical:
        st.warning('此设计方案对应历史需求条件或其他产品方向；可查看和导出，不能直接修订。')
    elif not same_source:
        st.info('这是其他来源需求单的方案；如需修订，请在上方选择其来源需求单版本。')
    if design.get('group_id'):
        group = sorted((v for v in run['design_versions'] if v.get('group_id') == design['group_id']), key=lambda v: v['variant'])
        st.write('同组方案对比')
        st.table([{'方案': f"{v['variant']}/{v['variants']} · 设计版本 {v['version']}", '名称': v['content']['title'], '概念': v['content']['concept'],
                   '配色': '、'.join(f"{c['name']} {c['hex']}" for c in v['content']['palette']),
                   '核心应用': '；'.join(a['name'] for a in v['content']['applications'] if a['priority'] == 'core')} for v in group])
        for v in group:
            st.markdown(f"方案 {v['variant']}：" + swatches(v['content']['palette']), unsafe_allow_html=True)
        st.caption('在上方“设计方案历史版本”中切换查看；选定一版后再修订或生成图稿。')
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
        changes = element_widgets({k: v for k, v in design['brief_snapshot']['evidence'].items() if 'element_id' in v}, prefix + 'elements', busy)
        revise = st.form_submit_button('生成设计方案新版本', disabled=busy or not work.llm.configured or historical or not same_source or not valid_source)
    if revise:
        launch(engine, 'demand_design_revise', identifier, design_id=design['id'], instruction=instruction, include_images=work.images.configured, element_changes=changes)
    from lab.design_pdf import export_pdf
    pdf_run, pdf_design = deepcopy(run), deepcopy(design)
    st.download_button('导出设计方案 PDF', lambda: export_pdf(work.demands, pdf_run, pdf_design),
        file_name=f"design-proposal-{identifier[:8]}-v{design['version']}.pdf", mime='application/pdf',
        key=prefix + 'pdf', on_click='ignore',
        help='包含当前设计版本的完整文字与已保存图稿，标注选用状态；不调用模型。')
    a, b = st.columns(2)
    a.download_button('下载设计方案 Markdown', demand_design.export_markdown(run, design),
        file_name=f"design-proposal-{identifier[:8]}-v{design['version']}.md", mime='text/markdown', key=prefix + 'md')
    b.download_button('下载设计方案 JSON', json.dumps(demand_design.export_bundle(run, design), ensure_ascii=False, indent=2),
        file_name=f"design-proposal-{identifier[:8]}-v{design['version']}.json", mime='application/json', key=prefix + 'json')
    image_step(engine, run, design, valid_source and same_source and not historical, op)


@st.dialog('图稿预览', width='large')
def artwork_preview(raw, title):
    st.write(title)
    st.image(raw, width='stretch')
    st.caption('概念图稿 · 可下载 PNG 查看原始像素')


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
    saved = {a.get('mode', 'application') for a in run.get('image_assets', []) if a['design_id'] == design['id']}
    # A pattern task saves only pattern drafts; open them instead of an empty application view.
    mode = st.radio('图稿类型', ['application', 'pattern'], format_func=lambda m: '应用效果图' if m == 'application' else '纹样初稿（可平铺单元）',
                    index=1 if saved == {'pattern'} else 0, horizontal=True, key=prefix + 'mode')
    if mode == 'pattern':
        index = 'pattern'
        st.caption('按方案配色、故事映射和方案包中的纹样方向生成三个方形纹样单元，供图案设计参考；未经接缝与分色检查。')
        has_patterns = any(a['design_id'] == design['id'] and a.get('mode') == 'pattern' for a in run.get('image_assets', []))
        # Each click starts a new batch; earlier batches stay in the history below.
        if st.button('再生成一批纹样初稿（3 张）' if has_patterns else '生成三版纹样初稿', key=prefix + 'pattern-generate',
                     type='secondary' if has_patterns else 'primary', disabled=not enabled or not work.llm.configured):
            launch(engine, 'demand_image_generate', run['id'], design_id=design['id'], mode='pattern')
    else:
        index = st.selectbox('图稿设计项', list(range(len(apps))),
            index=next((i for i, a in enumerate(apps) if a['priority'] == 'core'), 0),
            format_func=lambda i: apps[i]['name'] + ('（核心）' if apps[i]['priority'] == 'core' else '（可选）'), key=prefix + 'app')
        sizes = image_sizes(work.images.settings.provider)
        labels = dict(zip(sizes, ('方形', '横向', '纵向')))
        size = st.selectbox('图稿像素规格', sizes, format_func=lambda s: labels[s] + ' · ' + s.replace('x', '×'), key=prefix + work.images.settings.provider + '-size')
        has_drafts = any(a['design_id'] == design['id'] and a.get('mode', 'application') == 'application' and a['application_index'] == index
                         for a in run.get('image_assets', []))
        if st.button('再生成一批效果图（3 张）' if has_drafts else '生成三版效果图', key=prefix + 'generate',
                     type='secondary' if has_drafts else 'primary', disabled=not enabled or not work.llm.configured):
            launch(engine, 'demand_image_generate', run['id'], design_id=design['id'], application_index=index, size=size)
    step_feedback(engine, run['id'], op, demand_images.KINDS, 'images')
    if op and op['kind'] in demand_images.KINDS and op['status'] in ('failed', 'interrupted'):
        st.caption('点「重试失败阶段」只补生成未完成的图稿；服务端已生成但尚未保存时中断，重试可能再次调用并计费。')
    else:
        st.caption('每次点击都会新生成一批 3 张，不覆盖已有图稿；之前的批次可在下方「图稿批次与修订历史」中切换查看。')
    assets = [a for a in run.get('image_assets', []) if a['design_id'] == design['id'] and
              (a.get('mode') == 'pattern' if mode == 'pattern' else a.get('mode', 'application') == 'application' and a['application_index'] == index)]
    if not assets:
        return
    groups = list(dict.fromkeys(a['operation_id'] for a in reversed(assets)))
    group = st.selectbox('图稿批次与修订历史', groups,
        format_func=lambda g: next(f"图稿版本 {a['version']} 起 · {a['title']}" for a in assets if a['operation_id'] == g),
        key=prefix + str(index) + '-history-' + str(len(assets)))
    chosen = run.get('image_selections', {}).get(demand_images.selection_key(design['id'], index))
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
            st.image(raw, caption='平面概念图稿', width='stretch')
            if st.button('放大查看', key=asset['id'] + '-preview', width='stretch'):
                artwork_preview(raw, asset['title'])
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
