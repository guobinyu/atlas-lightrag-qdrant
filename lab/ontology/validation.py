"""Strict wire protocol, source anchoring and generated SHACL application shapes."""
import json
import unicodedata
from difflib import SequenceMatcher
from rdflib import Graph, BNode, RDF, RDFS, Namespace, Literal, URIRef
from pyshacl import validate as shacl_validate
from .profile import ALLOWED, PROPERTIES, VOCAB, DEFINITIONS, ancestors, uri

SH = Namespace('http://www.w3.org/ns/shacl#')


def parse_response(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('JSON 包含重复键')
            obj[key] = value
        return obj
    from lab.llm import json_text
    value = json.loads(json_text(raw), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非法数值')))
    if not isinstance(value, dict) or set(value) != {'entities', 'statements', 'deferred'}:
        raise ValueError('必须提供 entities、statements、deferred 三个数组')
    if any(not isinstance(value[k], list) for k in value) or any(len(value[k]) > 500 for k in value):
        raise ValueError('数组无效或超过单块 500 项限制')
    ids = set()
    for e in value['entities']:
        if not isinstance(e, dict) or not {'id', 'label', 'type'} <= e.keys() or e.keys() - {'id', 'label', 'type', 'concrete', 'uri'}:
            raise ValueError('实体字段无效')
        if any(not isinstance(e[k], str) or not e[k].strip() or len(e[k]) > 500 for k in ('id', 'label', 'type')) or e['id'] in ids:
            raise ValueError('实体 ID/标签无效或重复')
        if 'uri' in e and not isinstance(e['uri'], str):
            raise ValueError('实体 URI 必须是字符串')
        if 'concrete' in e and type(e['concrete']) is not bool:
            raise ValueError('concrete 必须是布尔值')
        ids.add(e['id'])
    for s in value['statements']:
        if not isinstance(s, dict) or not {'subject', 'predicate', 'object', 'quote'} <= s.keys() or s.keys() - {'subject', 'predicate', 'object', 'quote', 'start'}:
            raise ValueError('陈述字段无效')
        if any(not isinstance(s[k], str) for k in ('subject', 'predicate', 'quote')):
            raise ValueError('陈述字段必须为字符串')
        obj = s['object']
        if not isinstance(obj, str) and not (isinstance(obj, dict) and set(obj) == {'value', 'datatype'} and all(isinstance(v, str) for v in obj.values())):
            raise ValueError('宾语必须为实体 ID 或带 datatype 的字面量')
        if 'start' in s and type(s['start']) is not int:
            raise ValueError('引文偏移必须为整数')
        if s['predicate'] in PROPERTIES:
            s['predicate'] = str(uri(s['predicate']))
    if any(not isinstance(x, str) or len(x) > 3000 for x in value['deferred']):
        raise ValueError('deferred 必须为待建模说明字符串列表')
    return value


def entity_errors(entity):
    errors = []
    kind = entity['type']
    if kind not in ALLOWED:
        errors.append('类型不在应用配置白名单：' + kind)
    if kind in {'E7', 'E12'} and entity.get('concrete') is not True:
        errors.append('活动必须明确标记为正文记载的具体活动')
    if entity.get('uri'):
        term = VOCAB.get(entity['uri'])
        if not term or any(entity[k] != term[k] for k in ('type', 'label')):
            errors.append('受控概念 URI、类型或标签不匹配')
    return errors


def shapes():
    g = Graph()
    for p, (_, label, domain, range_) in PROPERTIES.items():
        subject_shape, object_shape = BNode(), BNode()
        for shape, target, expected in [(subject_shape, SH.targetSubjectsOf, domain), (object_shape, SH.targetObjectsOf, range_)]:
            g.add((shape, RDF.type, SH.NodeShape))
            g.add((shape, target, uri(p)))
            g.add((shape, SH['class'], uri(expected)))
            g.add((shape, SH.nodeKind, SH.IRI))
            g.add((shape, SH.message, Literal(label + ' 要求 ' + expected)))
    return g


def check(statement, entities, text, offset=0):
    errors = []
    for key in ('subject', 'object'):
        ref = statement[key]
        if not isinstance(ref, str) or ref not in entities:
            errors.append(key + ' 未引用有效实体（本配置的属性不接受字面量）')
        else:
            errors.extend(entity_errors(entities[ref]))
    property_code = next((p for p in PROPERTIES if str(uri(p)) == statement['predicate']), None)
    if property_code is None:
        errors.append('谓词不在标准 URI 白名单')
    anchor = locate_quote(statement['quote'], text, statement.get('start'), offset)
    if anchor['match_method'] == 'missing':
        errors.append('未找到足够相近的原文，请核对引文内容')
    report = ''
    if not errors:
        g = Graph()
        for ref in ('subject', 'object'):
            e = entities[statement[ref]]
            node = URIRef('urn:atlas:validation:' + ref)
            for parent in ancestors(e['type']):
                g.add((node, RDF.type, uri(parent)))
        g.add((URIRef('urn:atlas:validation:subject'), uri(property_code), URIRef('urn:atlas:validation:object')))
        conforms, _, report = shacl_validate(g, shacl_graph=shapes(), inference='none', allow_infos=False, allow_warnings=False)
        if not conforms:
            errors.append('SHACL 属性两端类型不符合应用配置')
    return {'valid': not errors, 'errors': errors, 'shacl': str(report), **anchor}


ANCHOR_VERSION = 'approximate-v1'


def normalized(text):
    """Retain a map to real source offsets, ignoring whitespace/punctuation only."""
    chars, indices = [], []
    for i, c in enumerate(text):
        if not c.isspace() and not unicodedata.category(c).startswith('P'):
            chars.append(c)
            indices.append(i)
    return ''.join(chars), indices


def locate_quote(quote, text, hint=None, offset=0):
    result = {'anchor_version': ANCHOR_VERSION, 'start': None, 'end': None,
              'matched_quote': '', 'match_method': 'missing', 'similarity': 0.0,
              'model_start': hint, 'warnings': []}
    if not quote.strip():
        return result

    def finish(start, end, method, score, count=1):
        result.update(start=start + offset, end=end + offset, matched_quote=text[start:end],
                      match_method=method, similarity=round(score, 4))
        if method != 'exact':
            result['warnings'].append('引文为近似匹配，请对照实际原文确认含义、人物和数字。')
        if hint is not None and hint != start:
            result['warnings'].append('已按原文重新定位，未采用模型给出的字符位置。')
        if count > 1:
            result['warnings'].append('原文中存在多处匹配，当前显示参考位置；请结合全文核对。')
        return result

    positions = [i for i in range(len(text)) if text.startswith(quote, i)]
    if positions:
        start = min(positions, key=lambda i: abs(i - hint)) if hint is not None else positions[0]
        return finish(start, start + len(quote), 'exact', 1.0, len(positions))
    q, _ = normalized(quote)
    body, indices = normalized(text)
    if not q or not body:
        return result
    positions = [i for i in range(len(body)) if body.startswith(q, i)]
    if positions:
        start = min(positions, key=lambda i: abs(indices[i] - hint)) if hint is not None else positions[0]
        return finish(indices[start], indices[start + len(q) - 1] + 1, 'normalized', 1.0, len(positions))
    # Short fragments are too weak for approximate matching. No semantic model call.
    if len(q) < 8:
        return result
    blocks = sorted(SequenceMatcher(None, q, body, autojunk=False).get_matching_blocks(), key=lambda b: b.size, reverse=True)[:16]
    starts = {max(0, b.b - b.a + delta) for b in blocks if b.size >= 3 for delta in (-2, -1, 0, 1, 2)}
    best = None
    delta = max(2, round(len(q) * .1))
    lengths = {max(1, len(q) + d) for d in (-delta, -2, -1, 0, 1, 2, delta)}
    for start in sorted(starts):
        for length in sorted(lengths):
            end = min(len(body), start + length)
            candidate = body[start:end]
            if not candidate:
                continue
            matcher = SequenceMatcher(None, q, candidate, autojunk=False)
            if matcher.quick_ratio() < .85:
                continue
            score = matcher.ratio()
            distance = abs(indices[start] - hint) if hint is not None else start
            rank = (score, -distance)
            if best is None or rank > best[0]:
                best = (rank, start, end)
    if best and best[0][0] >= .85:
        _, start, end = best
        return finish(indices[start], indices[end - 1] + 1, 'approximate', best[0][0])
    return result
