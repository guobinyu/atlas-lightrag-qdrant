"""30 synthetic review cases. These are not historical claims or user data.

Expected judgments are curated engineering fixtures, not independent expert review.
"""
from copy import deepcopy
from lab.ontology.profile import uri, TERM_BASE


def cases():
    result = []
    for domain, technique, material, artifact in [('silk', '织造', '丝线', '织锦样品'), ('tea', '制茶', '茶叶', '茶样'), ('wine', '酿造', '糯米', '黄酒样品')]:
        text = f'【合成测试】张甲实施了这次{technique}，使用{material}制作了这件{artifact}。'
        entities = [{'id': 'event', 'label': '本次'+technique, 'type': 'E12', 'concrete': True},
                    {'id': 'actor', 'label': '张甲', 'type': 'E21'},
                    {'id': 'material', 'label': material, 'type': 'E57'},
                    {'id': 'object', 'label': '这件'+artifact, 'type': 'E22'}]
        def wire(predicate='P14', subject='event', object='actor', quote=text, start=0):
            return {'entities': deepcopy(entities), 'statements': [{'subject': subject, 'predicate': str(uri(predicate)), 'object': object, 'quote': quote, 'start': start}], 'deferred': []}
        def add(name, payload, valid=True, source=text, expected=''):
            result.append({'id': domain+'-'+name, 'domain': domain, 'synthetic': True, 'text': source, 'response': payload, 'valid': valid, 'expected': expected})
        add('specific-event', wire(), expected='E12 → P14 → E21；方向正确；引文支持')
        add('material', wire('P126', object='material'), expected='E12 继承 E11，可使用 P126')
        add('product', wire('P108', object='object'), expected='E22 继承 E24')
        add('missing-time-place', wire(), expected='不生成时间地点，未知不等于否定')
        generic = f'【合成测试】{technique}是一种传统技艺。'
        add('general-method', {'entities': [{'id':'method','label':technique,'type':'E55','uri':TERM_BASE+domain+'/0'}], 'statements':[], 'deferred':[]}, source=generic, expected='仅 E55；不创造一次活动')
        bad = wire(); bad['entities'][0]['concrete'] = False
        add('invented-event', bad, False, source=generic, expected='通识描述不应生成具体活动')
        bad = wire(); bad['statements'][0]['quote'] = '此句不存在。'
        add('fabricated-quote', bad, False, expected='无对应引文不能发布')
        add('wrong-direction', wire(subject='actor', object='event'), False, expected='P14 方向颠倒，SHACL 拒绝')
        bad = wire(); bad['statements'][0]['start'] = 5
        add('wrong-offset', bad, True, expected='位置错误可按原文重新定位，仍需人工批准')
        homonyms = f'【合成测试】甲村的张甲与乙村的张甲分别实施了一次{technique}。'
        bad = wire(quote=homonyms); bad['entities'] += [{'id':'actor2','label':'张甲','type':'E21'}, {'id':'event2','label':'本次'+technique,'type':'E12','concrete':True}]
        bad['statements'].append({'subject':'event2','predicate':str(uri('P14')),'object':'actor2','quote':homonyms,'start':0})
        add('homonyms', bad, source=homonyms, expected='同名人物与活动保持不同 ID')
    return result
