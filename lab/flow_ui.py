"""One-way step flow shared by the hotspot prediction and custom design workbenches."""
from copy import deepcopy
from html import escape
import json

import streamlit as st

from lab.demand_ui import design_cards, element_widgets, evidence_view, history_view, image_step, launch, step_feedback
from lab.demands import brief_sections, current_brief, export_bundle, export_markdown, round_markdown, stale
from lab.ui_kit import category_icon

NUMBERS = '①②③④⑤'
STATE_LABELS = {'done': '已完成', 'current': '进行中', 'auto': '自动生成中', 'locked': '待完成'}
IMAGE_MODES = {'application': '应用效果图', 'none': '仅文字方案'}
DESIGN_KINDS = ('demand_design_generate', 'demand_design_revise', 'demand_pipeline')
# Changing a step makes everything from this point on historical.
IMPACT_ORDER = ('rounds', 'selection', 'brief', 'designs')


def last_op(run):
    ops = list(run['operations'].values())
    return ops[-1] if ops else None


def running(run, kinds):
    op = last_op(run)
    return bool(op and op['kind'] in kinds and op['status'] == 'running')


def latest_analysis(run):
    return next((c for c in reversed(run['clarifications']) if c['revision'] == run['revision']), None)


def current_round(run):
    if run['selection']:
        return next((b for b in run['candidate_rounds'] if b['id'] == run['selection']['round_id']), None)
    return next((b for b in reversed(run['candidate_rounds']) if not stale(run, b)), None)


def flow_bar(steps, states, label):
    items = ''.join(f'<li class="flow-step {state}"' + (' aria-current="step"' if state == 'current' else '') + '>'
                    f'<b>{NUMBERS[i]}</b><span>{steps[i]}</span><small>{STATE_LABELS[state]}</small></li>'
                    for i, state in enumerate(states))
    st.markdown(f'<ol class="flow-bar" aria-label="{escape(label)}">{items}</ol>', unsafe_allow_html=True)


def step_title(steps, i, state, note=''):
    st.markdown(f'<div class="flow-title {state}"><b>{NUMBERS[i]}</b> {steps[i]}'
                f'<small>{STATE_LABELS[state]}{" · " + escape(note) if note else ""}</small></div>', unsafe_allow_html=True)


def edit_key(prefix, run, i):
    return f"{prefix}-edit-{i}-{run['id']}"


def toggle(key):
    st.session_state[key] = not st.session_state.get(key, False)


def flash(prefix, message):
    # A toast raised right before st.rerun() is lost, so show it on the next run.
    st.session_state[prefix + '-flash'] = message


def show_flash(prefix):
    message = st.session_state.pop(prefix + '-flash', None)
    if message:
        st.toast(message, icon=':material/check_circle:')


def impact(run, start, brief_label='方案包'):
    """Results that become history when a step is changed; listed before anything is submitted."""
    level = IMPACT_ORDER.index(start)
    brief = current_brief(run)
    rounds = [b for b in run['candidate_rounds'] if not stale(run, b)]
    designs = [v for v in run.get('design_versions', []) if brief and v['brief_id'] == brief['id']]
    images = [a for a in run.get('image_assets', []) if any(a['design_id'] == v['id'] for v in designs)]
    items = []
    if level <= 0 and rounds:
        items.append(f'{len(rounds)} 轮候选')
    if level <= 1 and run['selection']:
        items.append('已选方向')
    if level <= 2 and brief:
        items.append(f"{brief_label}（版本 {brief['version']}）")
    if designs:
        items.append(f'{len(designs)} 版设计方案' + (f'及 {len(images)} 张图稿' if images else ''))
    if items:
        st.warning('提交后以下结果会变为历史版本，仍可在页面底部「版本记录」中查看：' + '、'.join(items))
    else:
        st.caption('目前没有后续结果，修改不会影响其他内容。')


def direction_lines(run):
    batch = current_round(run)
    candidate = next((c for c in batch['candidates'] if c['id'] == run['selection']['candidate_id']), None) if batch and run['selection'] else None
    if not candidate:
        return ['已选方向不可用，请重新选择。']
    lines = [f"{category_icon(candidate.get('requirements', {}).get('category', candidate['form']))} {candidate['name']}"]
    if candidate.get('requirements'):
        req = candidate['requirements']
        lines.append(req['category'] + (' · ' + '、'.join(req['crafts']) if req['crafts'] else '')
                     + f" · 爆款潜力 {candidate['potential']['score']}/5（模型判断）")
    return lines


def brief_lines(brief):
    package = brief['content'].get('package') or {}
    lines = [f"{brief['content']['title']}（版本 {brief['version']}）"]
    if package.get('pricing'):
        lines.append(f"定价参考 {package['pricing']['low']}–{package['pricing']['high']} 元／件 · 风险 {len(package.get('risks', []))} 项")
    return lines


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


def brief_more(engine, run, brief, busy, editing, prefix, edit, label='方案包'):
    files = {'方案包': 'package', '设计说明': 'design-brief'}.get(label, 'brief')
    with st.expander(f'修订{label}与导出', expanded=editing):
        if editing:
            impact(run, 'brief', label)
        key = f"{prefix}-brief-{brief['id']}-"
        with st.form(key + 'revise'):
            instruction = st.text_area('修订意见', max_chars=4000)
            changes = element_widgets({k: v for k, v in brief['evidence'].items() if 'element_id' in v}, key, busy)
            revise = st.form_submit_button(f'生成{label}新版本', disabled=busy or not engine.workbench.llm.configured)
        if revise:
            st.session_state.pop(edit, None)
            launch(engine, 'demand_revise', run['id'], brief_id=brief['id'], instruction=instruction, element_changes=changes)
        a, b = st.columns(2)
        a.download_button('下载 Markdown', export_markdown(run, brief), file_name=f"{files}-v{brief['version']}.md", mime='text/markdown', key=key + 'md')
        b.download_button('下载 JSON', json.dumps(export_bundle(run, brief), ensure_ascii=False, indent=2),
                          file_name=f"{files}-v{brief['version']}.json", mime='application/json', key=key + 'json')
        evidence_view(brief['evidence'])


def generate_options(engine, run, brief, busy, label, prefix, default_variants=1):
    work = engine.workbench
    key = f"{prefix}-design-{run['id']}-{brief['id']}-"
    with st.expander('生成选项', expanded=False):
        variants = st.selectbox('一次生成方案数', [1, 3], index=[1, 3].index(default_variants),
                                format_func=lambda n: '1 版' if n == 1 else '3 版对比（仅文字，选定一版后再出图）',
                                key=key + 'variants', disabled=busy)
        mode = st.radio('图稿', list(IMAGE_MODES), format_func=IMAGE_MODES.get, key=key + 'mode', horizontal=True,
                        disabled=busy or variants == 3 or not work.images.configured)
    if variants == 3:
        st.caption('一次生成 3 版文字方案供比较，选定一版后再生成效果图。')
    elif not work.images.configured:
        st.caption('尚未配置图片服务，本次只生成文字方案。')
    with_images = work.images.configured and variants == 1 and mode != 'none'
    if label is None:
        label = '生成三版设计方案' if variants == 3 else '生成设计与效果图' if with_images else '生成设计方案'
    if st.button(label, type='primary', icon=':material/auto_awesome:', disabled=busy or not work.llm.configured, key=key + 'generate'):
        inputs = {'brief_id': brief['id'], 'variants': variants, 'include_images': with_images}
        if with_images:
            inputs['image_mode'] = mode
        launch(engine, 'demand_design_generate', run['id'], **inputs)


def design_view(engine, run, brief, design, busy, prefix, default_variants=1, new_label=None):
    from lab import demand_design
    from lab.design_pdf import export_pdf
    work = engine.workbench
    op = last_op(run)
    step_feedback(engine, run['id'], op, DESIGN_KINDS, prefix + '-design')
    if running(run, DESIGN_KINDS):
        return
    if design is None:
        versions = [v for v in run.get('design_versions', []) if v['brief_id'] == brief['id'] and not stale(run, v)]
        if versions and versions[-1].get('group_id'):
            design_cards(engine, run, brief, busy, prefix=prefix, heading='选择一版设计方案')
            st.caption('确认一版后，在这一步继续生成效果图。')
        else:
            st.caption('依据：' + '；'.join(brief_lines(brief)))
            generate_options(engine, run, brief, busy, new_label, prefix, default_variants)
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
        key = f"{prefix}-design-{design['id']}-"
        with st.form(key + 'revise'):
            instruction = st.text_area('设计方案修改意见', placeholder='例如：主色改为墨绿，保留故事，简化构图。', max_chars=4000)
            changes = element_widgets({k: v for k, v in brief['evidence'].items() if 'element_id' in v}, key + 'elements', busy)
            revise = st.form_submit_button('生成设计方案新版本', disabled=busy or not work.llm.configured)
        if revise:
            launch(engine, 'demand_design_revise', run['id'], design_id=design['id'], instruction=instruction, element_changes=changes,
                   include_images=work.images.configured)
        pdf_run, pdf_design = deepcopy(run), deepcopy(design)
        a, b, c = st.columns(3)
        a.download_button('导出 PDF', lambda: export_pdf(work.demands, pdf_run, pdf_design), file_name=f"design-{run['id'][:8]}-v{design['version']}.pdf",
                          mime='application/pdf', key=key + 'pdf', on_click='ignore')
        b.download_button('下载 Markdown', demand_design.export_markdown(run, design), file_name=f"design-{run['id'][:8]}-v{design['version']}.md",
                          mime='text/markdown', key=key + 'md')
        c.download_button('下载 JSON', json.dumps(demand_design.export_bundle(run, design), ensure_ascii=False, indent=2),
                          file_name=f"design-{run['id'][:8]}-v{design['version']}.json", mime='application/json', key=key + 'json')
        st.divider()
        generate_options(engine, run, brief, busy, '重新生成设计方案', prefix, default_variants)


def version_record(run, brief_label='方案包'):
    from lab import demand_design
    from lab.campaign_ui import local_time
    with st.expander('版本记录', icon=':material/history:'):
        rounds, briefs, designs, log = st.tabs(['候选轮次', brief_label, '设计方案', '执行记录'])
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
                st.download_button('导出', export_markdown(run, brief), file_name=f"brief-v{brief['version']}.md", mime='text/markdown',
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
