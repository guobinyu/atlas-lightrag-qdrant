"""Capacity-checked candidates, product package, ranked topics, element changes, design variants and pattern drafts."""
from copy import deepcopy
from io import BytesIO
import json

import pytest
from pypdf import PdfReader

from lab import demand_images
from lab.demand_design import sections
from lab.demands import (capacity_violations, check_facts, check_package, export_bundle, export_markdown,
                         candidate_comparison, topic_evidence, CONTENT_FORMAT_VERSION)
from lab.demand_presets import TEMPLATES
from lab.design_pdf import export_pdf
from test_demands import demand, work, DemandModel, INPUT, start
from test_demand_images import images, png
from test_demand_ui import page, button, wait

CAPACITY = {'categories': ['丝巾', '礼盒包装'], 'crafts': ['数码印花'], 'lead_days': 20, 'monthly': 3000, 'moq': 100}
BUDGET = {'price_min': 100, 'price_max': 200, 'quantity': 500}
ELEMENT = {'element_id': 'e' * 32, 'type': 'motif', 'label': '牡丹纹', 'basis': 'documented', 'colors': [], 'themes': ['婚恋'],
           'score': 0.8, 'title': '文化素材·符号纹样：牡丹纹', 'chunk_id': 'culture:x', 'card': '符号纹样：牡丹纹', 'text': '牡丹常见于丝绸装饰'}


class V4Model(DemandModel):
    """Controls v4 fields per stage; everything else comes from the shared deterministic model."""
    def __init__(self, previous):
        super().__init__(previous)
        self.requirements = []
        self.package = {}
        self.refs = {}

    async def __call__(self, prompt, **kwargs):
        stage = prompt.split('：')[0]
        if stage in ('DEMAND_PATTERN_PROMPTS', 'DEMAND_IMAGE_PROMPTS'):
            self.calls.append(stage)
            self.inputs.append(json.loads(prompt.split('输入数据：\n')[1]))
            return json.dumps({'variants': [{'title': f'纹样{i}', 'prompt': f'缠枝骨架{i}'} for i in range(3)]}, ensure_ascii=False)
        value = json.loads(await super().__call__(prompt, **kwargs))
        if stage == 'DEMAND_CANDIDATES' and self.requirements:
            for item, req in zip(value['candidates'], self.requirements.pop(0)):
                item['requirements'] = req
        if stage == 'DEMAND_BRIEF':
            value['package'] = {**value['package'], **self.package}
            if 'brief' in self.refs:
                value['story_mapping'][0]['element_ref'] = self.refs['brief']
        if stage == 'DEMAND_DESIGN':
            data = self.inputs[-1]
            value['title'] = f"方案{len(data.get('distinct_from', [])) + 1}"
            if 'design' in self.refs:
                value['story_mapping'][0]['element_ref'] = self.refs['design']
        return json.dumps(value, ensure_ascii=False)


def req(category='丝巾', crafts=('数码印花',), lead=30, quantity=500):
    return {'category': category, 'crafts': list(crafts), 'lead_days': lead, 'quantity': quantity}


@pytest.fixture
def v4(demand):
    demand.llm = V4Model(demand.llm)
    return demand


def approved(work, **inputs):
    run = work.demands.create({**INPUT, **inputs})
    work.demands.approve(run['id'])
    return work.demands.load(run['id'])


def test_capacity_violations_cover_category_craft_lead_and_quantity():
    good = {'requirements': req(), 'capability_gaps': []}
    assert capacity_violations(good, CAPACITY) == []
    assert capacity_violations(good, {}) == []
    problems = capacity_violations({'requirements': req('旗袍', ('刺绣',), 10, 9000), 'capability_gaps': []}, CAPACITY)
    assert len(problems) == 4 and '旗袍' in problems[0] and '刺绣' in problems[1]
    assert capacity_violations({'requirements': req(crafts=('刺绣',)), 'capability_gaps': ['需新增刺绣外协']}, CAPACITY) == []
    assert '起订量' in capacity_violations({'requirements': req(quantity=20), 'capability_gaps': []}, CAPACITY)[0]


def test_violating_round_is_fixed_once_and_logged(v4):
    bad = [req('旗袍'), req(), req(crafts=('缂丝',))]
    v4.llm.requirements = [bad, [req(), req('礼盒包装'), req(crafts=('数码印花',))]]
    run = approved(v4, capacity=CAPACITY)
    run, status = start(v4, run, 'candidates')
    assert status['status'] == 'success'
    assert v4.llm.calls.count('DEMAND_CANDIDATES') == 2
    fix = v4.llm.inputs[-1]
    assert fix['candidates_to_fix'] == ['建议礼品0', '建议礼品2'] and len(fix['round_to_fix']) == 3
    batch = run['candidate_rounds'][0]
    assert batch['format_version'] == CONTENT_FORMAT_VERSION == 4
    assert [x['name'] for x in batch['capacity_log']] == ['建议礼品0', '建议礼品2']
    assert all(row['产能检查'] == '通过' for row in candidate_comparison(batch))
    assert candidate_comparison(batch)[0]['潜力（模型判断）'] == '3/5'


def test_still_violating_fails_with_reasons_and_retry_reuses_first_answer(v4):
    v4.llm.requirements = [[req('旗袍')] * 3, [req('旗袍')] * 3]
    run = approved(v4, capacity=CAPACITY)
    run, status = start(v4, run, 'candidates')
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    assert '修正后仍超出产能' in op['message'] and '旗袍' in op['message']
    assert 'candidates' in op['cache'] and not run['candidate_rounds']
    v4.llm.requirements = [[req(), req('礼盒包装'), req()]]
    run, status = start(v4, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    assert v4.llm.calls.count('DEMAND_CANDIDATES') == 3  # The first answer is cached, only the fix is retried.


def test_no_capacity_means_no_filtering(v4):
    run = approved(v4)
    run, status = start(v4, run, 'candidates')
    assert status['status'] == 'success'
    assert run['candidate_rounds'][0]['capacity_log'] == []
    assert candidate_comparison(run['candidate_rounds'][0])[0]['产能检查'] == '未设置产能参数'


def test_package_budget_overlap_export_and_merchant_lines(v4):
    run = approved(v4, capacity=CAPACITY, budget=BUDGET, template='alumni')
    v4.llm.requirements = [[req(), req('礼盒包装'), req()]]
    run, _ = start(v4, run, 'candidates')
    assert v4.llm.inputs[-1]['template_focus'] == TEMPLATES['alumni']['focus']
    v4.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    v4.llm.package = {'pricing': {'low': 300, 'high': 400, 'basis': '高端礼盒'}}
    run, status = start(v4, run, 'brief')
    assert status['status'] == 'failed'
    assert '没有重叠' in list(run['operations'].values())[-1]['responses'][-1]['error']
    v4.llm.package = {}
    run, status = start(v4, run, 'retry', operation_id=list(run['operations'])[-1])
    assert status['status'] == 'success'
    brief = run['briefs'][0]
    md = export_markdown(run, brief)
    for text in ('产品方案包与需求规格书', '定价参考', '参考估算，需报价验证', '[中] 交期紧张', '| 数量 | 待定 | 待定 |',
                 '可生产品类：丝巾、礼盒包装', '预算单价（元）：100–200', '场景模板：校友纪念品'):
        assert text in md
    assert export_bundle(run, brief)['schema_version'] == 4
    with pytest.raises(ValueError, match='风险'):
        check_package({'package': {**brief['content']['package'], 'risks': []}}, {}, {})


def test_calendar_and_manual_topics_select_generate_and_are_not_citable(v4):
    run = approved(v4, path='trend', capacity={'lead_days': 20})
    with pytest.raises(ValueError, match='120 天'):
        v4.demands.select_topic(run['id'], 'N-chunjie-2027-x')
    v4.demands.select_topic(run['id'], 'N-double11-2026')
    run, status = start(v4, run, 'candidates')
    assert status['status'] == 'success'
    assert 'DEMAND_SEARCH' not in v4.llm.calls
    context = run['candidate_rounds'][0]['context']
    assert context['topic']['kind'] == 'calendar' and context['topic_score']['note'] == '预测评分，非真实热度'
    assert context['evidence']['N-double11-2026']['basis'] == 'calendar'
    v4.demands.calendar.remove('double11-2026')
    run = v4.demands.load(run['id'])
    assert any(t['id'] == 'N-double11-2026' for t in v4.demands.topics(run))  # Snapshot stays listed.
    topic = v4.demands.add_manual_topic(run['id'], '国潮丝巾走红', '社交平台讨论增多', 'https://example.com/post', '2026-11-20')
    with pytest.raises(ValueError, match='http'):
        v4.demands.add_manual_topic(run['id'], '无效链接', url='file:///etc/passwd')
    v4.demands.select_topic(run['id'], topic['id'])
    run, status = start(v4, v4.demands.load(run['id']), 'candidates')
    assert status['status'] == 'success'
    evidence = run['candidate_rounds'][-1]['context']['evidence']
    assert evidence[topic['id']]['basis'] == 'user'
    with pytest.raises(ValueError, match='不能作为事实'):
        check_facts({'facts': [{'text': '走红', 'citations': [{'ref': topic['id'], 'quote': '国潮丝巾走红'}]}]}, evidence)
    with pytest.raises(ValueError, match='不能作为事实'):
        check_facts({'facts': [{'text': '大促', 'citations': [{'ref': 'N', 'quote': '双十一'}]}]},
                    {'N': topic_evidence({'kind': 'calendar', 'title': '双十一', 'fit': ''})})


def culture(work):
    async def context(question, hint=''):
        return [deepcopy(ELEMENT)]
    work.engine.culture_context = context


def briefed(work, **inputs):
    run = approved(work, **inputs)
    run, status = start(work, run, 'candidates')
    assert status['status'] == 'success'
    work.demands.select_candidate(run['id'], run['candidate_rounds'][0]['id'], 'C1')
    run, status = start(work, work.demands.load(run['id']), 'brief')
    assert status['status'] == 'success'
    return run


def test_element_changes_are_checked_on_brief_and_design(v4):
    culture(v4)
    run = briefed(v4)
    brief = run['briefs'][0]
    with pytest.raises(ValueError, match='本次资料'):
        start(v4, run, 'revise', brief_id=brief['id'], element_changes={'adopt': ['M9']})
    run, status = start(v4, run, 'revise', brief_id=brief['id'], element_changes={'adopt': ['M1'], 'style': '典雅'})
    assert status['status'] == 'failed'
    op = list(run['operations'].values())[-1]
    assert op['element_changes'] == {'adopt': ['M1'], 'remove': [], 'style': '典雅'}
    assert '采用文化素材：M1 牡丹纹' in op['instruction'] and '风格调整为典雅' in op['instruction']
    assert 'M1 需在 element_ref 中采用' in op['responses'][-1]['error']
    v4.llm.refs['brief'] = 'M1'
    run, status = start(v4, run, 'retry', operation_id=op['id'])
    assert status['status'] == 'success'
    revised = run['briefs'][-1]
    assert revised['content']['story_mapping'][0]['element_ref'] == 'M1'
    run, _ = start(v4, run, 'design_generate', brief_id=revised['id'])
    design = run['design_versions'][-1]
    run, status = start(v4, run, 'design_revise', design_id=design['id'], element_changes={'remove': ['M1']})
    assert status['status'] == 'failed' and 'M1 应已移除' in list(run['operations'].values())[-1]['responses'][-1]['error']


def test_three_design_variants_are_one_text_only_group(v4):
    run = briefed(v4)
    brief = run['briefs'][0]
    with pytest.raises(ValueError, match='只生成文字'):
        start(v4, run, 'design_generate', brief_id=brief['id'], variants=3, include_images=True)
    run, status = start(v4, run, 'design_generate', brief_id=brief['id'], variants=3)
    assert status['status'] == 'success'
    group = run['design_versions']
    assert [v['variant'] for v in group] == [1, 2, 3] and len({v['group_id'] for v in group}) == 1
    assert [v['content']['title'] for v in group] == ['方案1', '方案2', '方案3']
    assert len(v4.llm.inputs[-1]['distinct_from']) == 2
    assert not run.get('image_assets')
    assert '同组方案 2/3' in sections(run, group[1])[0][1]
    with pytest.raises(ValueError, match='1 版或 3 版'):
        start(v4, run, 'design_revise', design_id=group[0]['id'], instruction='改', variants=3)


def test_pattern_drafts_save_select_and_export(images):
    images.llm = V4Model(images.llm)
    run = briefed(images)
    run, status = start(images, run, 'design_generate', brief_id=run['briefs'][0]['id'], include_images=True, image_mode='pattern')
    assert status['status'] == 'success'
    assets = run['image_assets']
    assert len(assets) == 3 and {a['mode'] for a in assets} == {'pattern'}
    assert assets[0]['application_index'] == -1 and assets[0]['size'] == '1024x1024'
    data = images.llm.inputs[-1]
    assert data['pattern_directions'] == [{'direction': '校园剪影线描'}] and data['palette']
    assert '四方连续' in images.image_calls[0]['prompt']
    design = run['design_versions'][0]
    demand_images.select(images.demands, run['id'], assets[1]['id'])
    run = images.demands.load(run['id'])
    assert run['image_selections'] == {design['id'] + ':pattern': assets[1]['id']}
    run, status = start(images, run, 'image_revise', image_id=assets[1]['id'], instruction='减少留白')
    assert status['status'] == 'success' and run['image_assets'][-1]['mode'] == 'pattern'
    run, status = start(images, run, 'image_generate', design_id=design['id'], application_index=0)
    assert status['status'] == 'success'
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(export_pdf(images.demands, run, design))).pages)
    assert text.index('设计图稿') < text.index('纹样初稿') and '已选用' in text


def test_ui_template_capacity_topics_and_variants(v4):
    app = page(v4)
    app.selectbox(key='demand-new-template').select('wedding').run()
    assert not app.exception
    assert app.selectbox(key='demand-new-path').value == 'custom'
    assert app.text_input(key='demand-new-audience').value == TEMPLATES['wedding']['audience']
    app.text_area(key='demand-new-request').input('婚礼回礼丝巾')
    app.text_input(key='demand-new-industry').input('丝绸')
    app.text_area(key='demand-new-capabilities').input('真丝丝巾')
    app.multiselect(key='demand-new-cap-categories').set_value(['丝巾'])
    app.number_input(key='demand-new-cap-lead').set_value(20)
    app.number_input(key='demand-new-budget-max').set_value(200.0)
    button(app, '保存新任务').click().run()
    assert not app.exception
    run = v4.demands.runs()[0]
    assert run['inputs']['template'] == 'wedding' and run['inputs']['capacity'] == {'categories': ['丝巾'], 'lead_days': 20}
    assert run['inputs']['budget'] == {'price_max': 200.0}
    v4.demands.approve(run['id'])
    app.run()
    assert any('预测评分' in c.value for c in app.caption)
    next(t for t in app.text_input if t.label == '热点标题').input('婚礼丝巾回礼')
    button(app, '添加热点').click().run()
    assert not app.exception
    assert v4.demands.load(run['id'])['manual_topics'][0]['title'] == '婚礼丝巾回礼'
    v4.llm.requirements = [[req(crafts=())] * 3]
    for item in v4.llm.requirements[0]:
        item['crafts'] = []
    button(app, '生成新一轮产品候选').click().run()
    wait(v4)
    app.run()
    assert not app.exception
    assert any('产能检查' in str(t.value) for t in app.table)
