"""Automatically derive a campaign's brand/product direction from its evidence."""
from copy import deepcopy

FIELDS = {
    'name': '品牌名称', 'product': '产品方向', 'selling_points': '产品卖点',
    'audience': '目标人群', 'positioning': '品牌定位', 'scenarios': '使用场景',
    'constraints': '表达边界',
}

PROMPT = '''INFER_BRAND：先分析近期话题及知识库，再自动确定一组适合本次传播的品牌与产品信息。
不向用户提问，不要求输入、确认或审批。尊重用户需求中明确指定的信息。
知识库能明确识别已有品牌/产品时优先使用，但新闻中的主办方、报道媒体或其他品牌不自动视为用户品牌。
没有明确品牌时创造一个贴合热点的策划品牌名称和产品方向，标记为策划推断；不要杜撰为真实存在、已注册或已获得认证的品牌。
没有合格近期话题的常规策划只使用用户需求与知识库，不宣称基于近期热点。
返回对象，必须包含七个非空字符串字段：name（品牌名称）、product（产品方向）、selling_points（卖点）、audience（人群）、positioning（定位）、scenarios（场景）、constraints（表达边界）。
同时返回 basis 数组，为每个字段提供且仅提供一项：{"field":"name","kind":"资料提取|策划推断","reason":"选择理由","citations":[{"ref":"S1或I1","quote":"连续原文摘录"}]}。
资料提取必须提供可定位的原文引用。推断可以没有引用；不得把产品建议包装为既有产品属性或认证。
'''


def normalize_profile(raw, evidence):
    """Invalid citations never become facts; downgrade automatically, without a gate."""
    if any(not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > 5000 for key in FIELDS):
        raise ValueError('自动推理的品牌与产品信息不完整，可重试品牌推理阶段。')
    basis = raw.get('basis')
    if not isinstance(basis, list) or len(basis) != len(FIELDS):
        raise ValueError('品牌推理缺少逐项依据。')
    if any(not isinstance(item, dict) or not isinstance(item.get('field'), str) for item in basis) or {item.get('field') for item in basis} != set(FIELDS):
        raise ValueError('品牌推理的依据字段无效。')
    profile = {key: raw[key].strip() for key in FIELDS}
    profile['basis'] = []
    for item in basis:
        if item.get('kind') not in ('资料提取', '策划推断') or not isinstance(item.get('reason'), str) or not isinstance(item.get('citations'), list):
            raise ValueError('品牌推理的依据格式无效。')
        citations = []
        for cite in item['citations']:
            if not isinstance(cite, dict) or not isinstance(cite.get('ref'), str) or not isinstance(cite.get('quote'), str):
                continue
            source = evidence.get(cite['ref'], {})
            text = source.get('text') or source.get('content') or ''
            if cite['quote'].strip() and cite['quote'] in text:
                citations.append({'ref': cite['ref'], 'quote': cite['quote']})
        entry = {**deepcopy(item), 'citations': citations}
        if item['kind'] == '资料提取' and (not citations or len(citations) != len(item['citations'])):
            entry.update(kind='策划推断', reason=item['reason'] + '；原文依据不足，作为策划设定使用。')
        profile['basis'].append(entry)
    profile['mode'] = 'automatic'
    return profile


def brand_values(run):
    """Keep old task snapshots readable; never reuse them as new-task defaults."""
    return run.get('brand_profile') or run['inputs'].get('brand', {})
