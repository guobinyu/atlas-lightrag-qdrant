"""Deterministic design content; not evidence of real design quality."""
def proposal(data):
    color = '#173F36' if '墨绿' in data['instruction'] else '#F3EEE4'
    source = data['source_brief']
    deliveries = data['structured_deliverables'] or [{'id': '', 'name': '原需求单的标签设计补充建议', 'priority': 'core'}]
    return {
        'title': '重逢留白文字设计方案', 'concept': '以留白供校友写下重逢记忆，主题字靠左，桂花线描置于右下角。',
        'audience_scene': '校友聚会后带走纪念；尺寸尚未确认，先按比例布局。',
        'story': source['story'],
        'story_mapping': source.get('story_mapping') or [{'meaning': '重逢', 'element': '相接的桂花枝', 'placement': '标签角部', 'reason': '表达再相聚'}],
        'composition': '上部三分之一留给主题字，下部留手写区，辅助枝条避开文字，留白约一半。',
        'palette': [{'role': 'main', 'name': '墨绿' if color == '#173F36' else '米白', 'hex': color, 'usage': '盒面与卡片底色'},
                    {'role': 'secondary', 'name': '灰绿', 'hex': '#414B45', 'usage': '主题字与线描'},
                    {'role': 'accent', 'name': '桂黄', 'hex': '#B68B43', 'usage': '少量花点'}],
        'typography': '主题字用疏朗宋体风格，副文字体约为主题字一半；品牌位留空。',
        'copywriting': [{'text': '重逢，留一行给今天', 'placement': '标签上部与盒盖左侧', 'role': '主题标题'}],
        'applications': [{'deliverable_id': d['id'], 'name': d['name'], 'priority': d['priority'],
                          'layout': '标题靠左上、插图靠右下，中心留白用于填写届别与日期。',
                          'graphics': '同一桂花枝在边角缩放，避免跨过手写区。', 'palette_refs': [color, '#414B45'],
                          'copy': '上部写“重逢，留一行给今天”，品牌与法定文案以占位框表示。',
                          'materials': '先按纸面设计建议推进；实际材质、尺寸和印刷效果待确认打样。'} for d in deliveries],
        'handoff': '先完成比例构图、线描和配色；尺寸和品牌文件到位后适配，背标文案由商家提供。',
        'assumptions': ['配色与材质仅为建议，打样验证。'] + ([] if data['structured_deliverables'] else ['旧版没有结构化交付，应用为据原文提出的补充建议。']),
        'followups': [{'item': '品牌文件与尺寸', 'category': '资料补齐', 'owner': '商家', 'timing': '精确排版前', 'approach': '先按占位和比例完成概念'}],
        'facts': [],
    }
